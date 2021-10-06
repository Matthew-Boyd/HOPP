from typing import Optional, Union, Sequence

import rapidjson                # NOTE: install 'python-rapidjson' NOT 'rapidjson'

import pandas as pd
import numpy as np
import datetime
import os

from hybrid.pySSC_daotk.ssc_wrap import ssc_wrap
import PySAM.Singleowner as Singleowner

from hybrid.dispatch.power_sources.csp_dispatch import CspDispatch
from hybrid.power_source import *
from hybrid.sites import SiteInfo


class Csp_Outputs():
    def __init__(self):
        self.ssc_time_series = {}
        self.dispatch = {}

    def update_from_ssc_output(self, ssc_outputs):
        seconds_per_step = int(3600/ssc_outputs['time_steps_per_hour'])
        ntot = int(ssc_outputs['time_steps_per_hour'] * 8760)
        is_empty = (len(self.ssc_time_series) == 0)
        i = int(ssc_outputs['time_start'] / seconds_per_step) 
        n = int((ssc_outputs['time_stop'] - ssc_outputs['time_start'])/seconds_per_step)

        if is_empty:
            for name, val in ssc_outputs.items():
                if isinstance(val, list) and len(val) == ntot:  
                    self.ssc_time_series[name] = [0.0]*ntot
        
        for name in self.ssc_time_series.keys():
            self.ssc_time_series[name][i:i+n] = ssc_outputs[name][0:n]

    def store_dispatch_outputs(self, dispatch: CspDispatch, n_periods: int, sim_start_time: int):
        outputs_keys = ['available_thermal_generation', 'cycle_ambient_efficiency_correction', 'condenser_losses',
                        'thermal_energy_storage', 'receiver_startup_inventory', 'receiver_thermal_power',
                        'receiver_startup_consumption', 'is_field_generating', 'is_field_starting', 'incur_field_start',
                        'cycle_startup_inventory', 'system_load', 'cycle_generation', 'cycle_thermal_ramp',
                        'cycle_thermal_power', 'is_cycle_generating', 'is_cycle_starting', 'incur_cycle_start']

        is_empty = (len(self.dispatch) == 0)
        if is_empty:
            for key in outputs_keys:
                self.dispatch[key] = [0.0] * 8760

        for key in outputs_keys:
            self.dispatch[key][sim_start_time: sim_start_time + n_periods] = getattr(dispatch, key)[0: n_periods]


class CspPlant(PowerSource):
    _system_model: None
    _financial_model: Singleowner
    # _layout: TroughLayout
    _dispatch: CspDispatch

    def __init__(self,
                 name: str,
                 tech_name: str,
                 site: SiteInfo,
                 financial_model: Singleowner,
                 csp_config: dict):
        """

        :param trough_config: dict, with keys ('system_capacity_kw', 'solar_multiple', 'tes_hours')
        """
        required_keys = ['cycle_capacity_kw', 'solar_multiple', 'tes_hours']
        if any(key not in csp_config.keys() for key in required_keys):
            is_missing = [key not in csp_config.keys() for key in required_keys]
            missing_keys = [missed_key for (missed_key, missing) in zip(required_keys, is_missing) if missing]
            raise ValueError(type(self).__name__ + " requires the following keys: " + str(missing_keys))

        self.name = name
        self.site = site

        self._financial_model = financial_model
        self._layout = None
        self._dispatch: CspDispatch = None
        self.set_construction_financing_cost_per_kw(0)

        # TODO: Should 'SSC' object be a protected attr
        # Initialize ssc and get weather data
        self.ssc = ssc_wrap(
            wrapper='pyssc',  # ['pyssc' | 'pysam']
            tech_name=tech_name,  # ['tcsmolten_salt' | 'trough_physical]
            financial_name=None,
            defaults_name=None)  # ['MSPTSingleOwner' | 'PhysicalTroughSingleOwner']  NOTE: not used for pyssc
        self.initialize_params()

        self.year_weather_df = self.tmy3_to_df()  # read entire weather file

        self.cycle_capacity_kw: float = csp_config['cycle_capacity_kw']
        self.solar_multiple: float = csp_config['solar_multiple']
        self.tes_hours: float = csp_config['tes_hours']

        self.cycle_efficiency_tables = self.get_cycle_efficiency_tables()
        self.plant_state = self.set_initial_plant_state()
        self.update_ssc_inputs_from_plant_state()

        self.outputs = Csp_Outputs()

    def param_file_paths(self, relative_path):
        cwd = os.path.dirname(os.path.abspath(__file__))
        data_path = os.path.join(cwd, relative_path)
        for key in self.param_files.keys():
            filename = self.param_files[key]
            self.param_files[key] = os.path.join(data_path, filename)

    def initialize_params(self):
        self.set_params_from_files()
        self.ssc.set({'time_steps_per_hour': 1})  # FIXME: defaults to 60
        n_steps_year = int(8760 * self.ssc.get('time_steps_per_hour'))
        self.ssc.set({'sf_adjust:hourly': n_steps_year * [0]})

    def tmy3_to_df(self):
        # NOTE: be careful of leading spaces in the column names, they are hard to catch and break the parser
        df = pd.read_csv(self.site.solar_resource.filename, sep=',', skiprows=2, header=0)
        date_cols = ['Year', 'Month', 'Day', 'Hour', 'Minute']
        df.index = pd.to_datetime(df[date_cols])
        df.index.name = 'datetime'
        df.drop(date_cols, axis=1, inplace=True)

        df.index = df.index.map(lambda t: t.replace(year=df.index[0].year))  # normalize all years to that of 1/1
        df = df[df.columns.drop(list(df.filter(regex='Unnamed')))]  # drop unnamed columns (which are empty)

        def get_weatherfile_location(tmy3_path):
            df_meta = pd.read_csv(tmy3_path, sep=',', header=0, nrows=1)
            return {
                'latitude': float(df_meta['Latitude'][0]),
                'longitude': float(df_meta['Longitude'][0]),
                'timezone': int(df_meta['Time Zone'][0]),
                'elevation': float(df_meta['Elevation'][0])
            }

        location = get_weatherfile_location(self.site.solar_resource.filename)
        df.attrs.update(location)
        return df

    def set_params_from_files(self):
        # Loads default case
        with open(self.param_files['tech_model_params_path'], 'r') as f:
            ssc_params = rapidjson.load(f)
        self.ssc.set(ssc_params)

        # NOTE: Don't set if passing weather data in via solar_resource_data
        # ssc.set({'solar_resource_file': param_files['solar_resource_file_path']})

        dispatch_factors_ts = np.array(pd.read_csv(self.param_files['dispatch_factors_ts_path']))
        self.ssc.set({'dispatch_factors_ts': dispatch_factors_ts})
        # TODO: remove dispatch factor file and use site
        # self.ssc.set({'dispatch_factors_ts': self.site.elec_prices.data})  # returning a empty array...

        ud_ind_od = np.array(pd.read_csv(self.param_files['ud_ind_od_path']))
        self.ssc.set({'ud_ind_od': ud_ind_od})

        wlim_series = np.array(pd.read_csv(self.param_files['wlim_series_path']))
        self.ssc.set({'wlim_series': wlim_series})

    def set_weather(self, weather_df: pd.DataFrame,
                    start_datetime: datetime = None,
                    end_datetime: datetime = None):
        """
        Sets 'solar_resource_data' for pySSC simulation. If start and end (datetime) are not provided, full year is
        assumed.
        :param weather_df: weather information
        :param start_datetime: start of pySSC simulation (datetime)
        :param end_datetime: end of pySSC simulation (datetime)
        """
        weather_timedelta = weather_df.index[1] - weather_df.index[0]
        weather_time_steps_per_hour = int(1 / (weather_timedelta.total_seconds() / 3600))
        ssc_time_steps_per_hour = self.ssc.get('time_steps_per_hour')
        if weather_time_steps_per_hour != ssc_time_steps_per_hour:
            raise Exception('Configured time_steps_per_hour ({x}) is not that of weather file ({y})'.format(
                x=ssc_time_steps_per_hour, y=weather_time_steps_per_hour))

        if start_datetime is None and end_datetime is None:
            if len(weather_df) != ssc_time_steps_per_hour * 8760:
                raise Exception('Full year weather dataframe required if start and end datetime are not provided')
            weather_df_part = weather_df
        else:
            weather_year = weather_df.index[0].year
            if start_datetime.year != weather_year:
                print('Replacing start and end years ({x}) with weather file\'s ({y}).'.format(
                    x=start_datetime.year, y=weather_year))
                start_datetime = start_datetime.replace(year=weather_year)
                end_datetime = end_datetime.replace(year=weather_year)

            if start_datetime < weather_df.index[0]:
                start_datetime = weather_df.index[0]

            if end_datetime <= start_datetime:
                end_datetime = start_datetime + weather_timedelta

            weather_df_part = weather_df[start_datetime:(
                        end_datetime - weather_timedelta)]  # times in weather file are the start (or middle) of timestep

        def weather_df_to_ssc_table(weather_df):
            rename_from_to = {
                'Tdry': 'Temperature',
                'Tdew': 'Dew Point',
                'RH': 'Relative Humidity',
                'Pres': 'Pressure',
                'Wspd': 'Wind Speed',
                'Wdir': 'Wind Direction'
            }
            weather_df = weather_df.rename(columns=rename_from_to)

            solar_resource_data = {}
            solar_resource_data['tz'] = weather_df.attrs['timezone']
            solar_resource_data['elev'] = weather_df.attrs['elevation']
            solar_resource_data['lat'] = weather_df.attrs['latitude']
            solar_resource_data['lon'] = weather_df.attrs['longitude']
            solar_resource_data['year'] = list(weather_df.index.year)
            solar_resource_data['month'] = list(weather_df.index.month)
            solar_resource_data['day'] = list(weather_df.index.day)
            solar_resource_data['hour'] = list(weather_df.index.hour)
            solar_resource_data['minute'] = list(weather_df.index.minute)
            solar_resource_data['dn'] = list(weather_df['DNI'])
            solar_resource_data['df'] = list(weather_df['DHI'])
            solar_resource_data['gh'] = list(weather_df['GHI'])
            solar_resource_data['wspd'] = list(weather_df['Wind Speed'])
            solar_resource_data['tdry'] = list(weather_df['Temperature'])
            solar_resource_data['pres'] = list(weather_df['Pressure'])
            solar_resource_data['tdew'] = list(weather_df['Dew Point'])

            def pad_solar_resource_data(solar_resource_data):
                datetime_start = datetime.datetime(
                    year=solar_resource_data['year'][0],
                    month=solar_resource_data['month'][0],
                    day=solar_resource_data['day'][0],
                    hour=solar_resource_data['hour'][0],
                    minute=solar_resource_data['minute'][0])
                n = len(solar_resource_data['dn'])
                if n < 2:
                    timestep = datetime.timedelta(hours=1)  # assume 1 so minimum of 8760 results
                else:
                    datetime_second_time = datetime.datetime(
                        year=solar_resource_data['year'][1],
                        month=solar_resource_data['month'][1],
                        day=solar_resource_data['day'][1],
                        hour=solar_resource_data['hour'][1],
                        minute=solar_resource_data['minute'][1])
                    timestep = datetime_second_time - datetime_start
                steps_per_hour = int(3600 / timestep.seconds)
                # Substitute a non-leap year (2009) to keep multiple of 8760 assumption:
                i0 = int((datetime_start.replace(year=2009) - datetime.datetime(2009, 1, 1, 0, 0,
                                                                                0)).total_seconds() / timestep.seconds)
                diff = 8760 * steps_per_hour - n
                front_padding = [0] * i0
                back_padding = [0] * (diff - i0)

                if diff > 0:
                    for k in solar_resource_data:
                        if isinstance(solar_resource_data[k], list):
                            solar_resource_data[k] = front_padding + solar_resource_data[k] + back_padding
                    return solar_resource_data
                else:
                    return solar_resource_data

            solar_resource_data = pad_solar_resource_data(solar_resource_data)
            return solar_resource_data

        self.ssc.set({'solar_resource_data': weather_df_to_ssc_table(weather_df_part)})

    @staticmethod
    def get_plant_state_io_map() -> dict:
        raise NotImplementedError

    def set_initial_plant_state(self) -> dict:
        io_map = self.get_plant_state_io_map()
        plant_state = {k: 0 for k in io_map.keys()}
        plant_state['rec_op_mode_initial'] = 0  # Receiver initially off
        plant_state['pc_op_mode_initial'] = 3  # Cycle initially off
        plant_state['pc_startup_time_remain_init'] = self.ssc.get('startup_time')
        plant_state['pc_startup_energy_remain_initial'] = self.ssc.get('startup_frac')*self.cycle_thermal_rating*1000.
        plant_state['sim_time_at_last_update'] = 0.0
        plant_state['T_tank_cold_init'] = self.htf_cold_design_temperature
        plant_state['T_tank_hot_init'] = self.htf_hot_design_temperature
        plant_state['pc_startup_energy_remain_initial'] = (self.value('startup_frac') * self.cycle_thermal_rating
                                                           * 1e6)  # MWh -> kWh
        return plant_state

    def set_plant_state_from_ssc_outputs(self, ssc_outputs, seconds_relative_to_start):
        time_steps_per_hour = self.ssc.get('time_steps_per_hour')
        time_start = self.ssc.get('time_start')
        # Note: values returned in ssc_outputs are at the front of the output arrays
        idx = round(seconds_relative_to_start/3600) * int(time_steps_per_hour) - 1
        io_map = self.get_plant_state_io_map()
        for ssc_input, output in io_map.items():
            if ssc_input == 'T_out_scas_initial':
                self.plant_state[ssc_input] = ssc_outputs[output]
            else:
                self.plant_state[ssc_input] = ssc_outputs[output][idx]
        # Track time at which plant state was last updated
        self.plant_state['sim_time_at_last_update'] = time_start + seconds_relative_to_start
        return

    def update_ssc_inputs_from_plant_state(self):
        state = self.plant_state.copy()
        state.pop('sim_time_at_last_update')
        state.pop('heat_into_cycle')
        self.ssc.set(state)
        return

    def get_cycle_efficiency_tables(self) -> dict:
        """
        Gets off-design cycle performance tables from pySSC.
        :return cycle_efficiency_tables: if tables exist, tables are return,
                                        else if user defined cycle, tables are calculated,
                                        else return emtpy dictionary with warning
        """
        start_datetime = datetime.datetime(self.year_weather_df.index[0].year, 1, 1, 0, 0, 0)  # start of first timestep
        self.set_weather(self.year_weather_df, start_datetime, start_datetime)  # only one weather timestep is needed
        self.ssc.set({'time_start': 0})
        self.ssc.set({'time_stop': 0})
        ssc_outputs = self.ssc.execute()

        required_tables = ['cycle_eff_load_table', 'cycle_eff_Tdb_table', 'cycle_wcond_Tdb_table']
        if all(table in ssc_outputs for table in required_tables):
            return {table: ssc_outputs[table] for table in required_tables}
        if ssc_outputs['pc_config'] == 1:
            # Tables not returned from ssc, but can be taken from user-defined cycle inputs
            return {'ud_ind_od': ssc_outputs['ud_ind_od']}
        else:
            print('WARNING: Cycle efficiency tables not found. Dispatch optimization will assume a constant cycle '
                  'efficiency and no ambient temperature dependence.')
            return {}

    def simulate_with_dispatch(self, n_periods: int, sim_start_time: int = None):
        """
        Step through dispatch solution and simulate trough system
        """
        # Set up start and end time of simulation
        start_datetime, end_datetime = CspDispatch.get_start_end_datetime(sim_start_time, n_periods)
        self.value('time_start', CspDispatch.seconds_since_newyear(start_datetime))
        self.value('time_stop', CspDispatch.seconds_since_newyear(end_datetime))

        self.set_dispatch_targets(n_periods)
        self.update_ssc_inputs_from_plant_state()

        # Simulate
        results = self.ssc.execute()
        if not results["cmod_success"]:
            raise ValueError('PySSC simulation failed...')

        # Save plant state at end of simulation
        simulation_time = (end_datetime - start_datetime).total_seconds()
        self.set_plant_state_from_ssc_outputs(results, simulation_time)

        # Save simulation output
        self.outputs.update_from_ssc_output(results)
        self.outputs.store_dispatch_outputs(self.dispatch, n_periods, sim_start_time)

    def set_dispatch_targets(self, n_periods: int):
        """Set pySSC targets using dispatch model solution."""
        # Set targets
        dis = self.dispatch

        dispatch_targets = {'is_dispatch_targets': 1,
                            # Receiver on, startup, (or standby - NOT in dispatch currently)
                            'is_rec_su_allowed_in': [1 if (dis.is_field_generating[t] + dis.is_field_starting[t]) > 0.01
                                                     else 0 for t in range(n_periods)],
                            # Receiver standby - NOT in dispatch currently
                            'is_rec_sb_allowed_in': [0 for t in range(n_periods)],
                            # Cycle on or startup
                            'is_pc_su_allowed_in': [1 if (dis.is_cycle_generating[t] + dis.is_cycle_starting[t]) > 0.01
                                                    else 0 for t in range(n_periods)],
                            # Cycle standby - NOT in dispatch currently
                            'is_pc_sb_allowed_in': [0 for t in range(n_periods)],
                            # Cycle start up thermal power
                            'q_pc_target_su_in': [dis.allowable_cycle_startup_power if dis.is_cycle_starting[t] > 0.01
                                                  else 0.0 for t in range(n_periods)],
                            # Cycle thermal power
                            'q_pc_target_on_in': dis.cycle_thermal_power[0:n_periods],
                            # Cycle max thermal power allowed
                            'q_pc_max_in': [self.cycle_thermal_rating for t in range(n_periods)]}
        self.ssc.set(dispatch_targets)

    def get_design_storage_mass(self):
        """Returns active storage mass [kg]"""
        q_pb_design = self.cycle_thermal_rating
        e_storage = q_pb_design * self.tes_hours * 1000.  # Storage capacity (kWht)
        cp = self.get_cp_htf(0.5 * (self.htf_hot_design_temperature + self.htf_cold_design_temperature)) * 1.e-3  # kJ/kg/K
        m_storage = e_storage * 3600. / cp / (self.htf_hot_design_temperature - self.htf_cold_design_temperature)
        return m_storage

    def get_cycle_design_mass_flow(self):
        q_des = self.cycle_thermal_rating  # MWt
        cp_des = self.get_cp_htf(0.5 * (self.htf_hot_design_temperature + self.htf_cold_design_temperature))  # J/kg/K
        m_des = q_des * 1.e6 / (cp_des * (self.htf_hot_design_temperature - self.htf_cold_design_temperature))  # kg/s
        return m_des

    def get_cp_htf(self, TC):
        """Returns specific heat at temperature TC in [J/kg/K]"""
        # TODO: add a field option or something for troughs
        #  Troughs: TES "store_fluid", Field HTF "Fluid"
        #  Ask Matt is 'Fluid' always driving the power cycle
        fluid_name_map = {'TowerPlant': 'rec_htf', 'TroughPlant': 'Fluid'}
        tes_fluid = self.value(fluid_name_map[type(self).__name__])

        TK = TC + 273.15
        if tes_fluid == 17:
            return (-1.0e-10 * (TK ** 3) + 2.0e-7 * (TK ** 2) + 5.0e-6 * TK + 1.4387) * 1000.  # J/kg/K
        elif tes_fluid == 18:
            return 1443. + 0.172 * (TK - 273.15)
        elif tes_fluid == 21:
            return (1.509 + 0.002496 * TC + 0.0000007888 * (TC ** 2)) * 1000.
        else:
            print('HTF %d not recognized' % tes_fluid)
            return 0.0

    def set_construction_financing_cost_per_kw(self, construction_financing_cost_per_kw):
        # TODO: CSP doesn't scale per kw -> need to update?
        self._construction_financing_cost_per_kw = construction_financing_cost_per_kw

    def get_construction_financing_cost(self) -> float:
        cf = ssc_wrap('pyssc', 'cb_construction_financing', None)
        with open(self.param_files['cf_params_path'], 'r') as f:
            params = rapidjson.load(f)
        cf.set(params)
        cf.set({'total_installed_cost': self.calculate_total_installed_cost()})
        outputs = cf.execute()
        construction_financing_cost = outputs['construction_financing_cost']
        return outputs['construction_financing_cost']

    def calculate_total_installed_cost(self) -> float:
        raise NotImplementedError

    def simulate(self, project_life: int = 25, skip_fin=False):
        """
        Run the system
        """
        raise NotImplementedError

    def simulate_financials(self, project_life):
        if project_life > 1:
            self._financial_model.Lifetime.system_use_lifetime_output = 1
        else:
            self._financial_model.Lifetime.system_use_lifetime_output = 0
        self._financial_model.FinancialParameters.analysis_period = project_life

        nameplate_capacity_kw = self.cycle_capacity_kw * self.ssc.get('gross_net_conversion_factor')  # TODO: avoid using ssc data here?
        self._financial_model.value("system_capacity", nameplate_capacity_kw)
        self._financial_model.value("cp_system_nameplate", nameplate_capacity_kw/1000)
        self._financial_model.value("total_installed_cost", self.calculate_total_installed_cost())
        self._financial_model.value("construction_financing_cost", self.get_construction_financing_cost())
        
        self._financial_model.Revenue.ppa_soln_mode = 1

        if len(self.generation_profile) == self.site.n_timesteps:
            single_year_gen = self.generation_profile
            self._financial_model.SystemOutput.gen = list(single_year_gen) * project_life

            self._financial_model.SystemOutput.system_pre_curtailment_kwac = list(single_year_gen) * project_life
            self._financial_model.SystemOutput.annual_energy_pre_curtailment_ac = sum(single_year_gen)

        self._financial_model.execute(0)
        logger.info("{} simulation executed".format(str(type(self).__name__)))

    def value(self, var_name, var_value=None):
        attr_obj = None
        ssc_value = None
        if var_name in self.__dir__():
            attr_obj = self
        if not attr_obj:
            for a in self._financial_model.__dir__():
                group_obj = getattr(self._financial_model, a)
                try:
                    if var_name in group_obj.__dir__():
                        attr_obj = group_obj
                        break
                except:
                    pass
        if not attr_obj:
            try:
                ssc_value = self.ssc.get(var_name)
                attr_obj = self.ssc
            except:
                pass
        if not attr_obj:
            raise ValueError("Variable {} not found in technology or financial model {}".format(
                var_name, self.__class__.__name__))

        if var_value is None:
            if ssc_value is None:
                return getattr(attr_obj, var_name)
            else:
                return ssc_value
        else:
            try:
                if ssc_value is None:
                    setattr(attr_obj, var_name, var_value)
                else:
                    self.ssc.set({var_name: var_value})
            except Exception as e:
                raise IOError(f"{self.__class__}'s attribute {var_name} could not be set to {var_value}: {e}")

    @property
    def _system_model(self):
        """Used for dispatch to mimic other dispatch class building in hybrid dispatch builder"""
        return self

    @property
    def system_capacity_kw(self) -> float:
        return self.cycle_capacity_kw

    @system_capacity_kw.setter
    def system_capacity_kw(self, size_kw: float):
        """
        Sets the power cycle capacity and updates the system model
        :param size_kw:
        :return:
        """
        self.cycle_capacity_kw = size_kw

    @property
    def cycle_capacity_kw(self) -> float:
        """ P_ref is in [MW] returning [kW] """
        return self.ssc.get('P_ref') * 1000.

    @cycle_capacity_kw.setter
    def cycle_capacity_kw(self, size_kw: float):
        """
        Sets the power cycle capacity and updates the system model TODO:, cost and financial model
        :param size_kw:
        :return:
        """
        self.ssc.set({'P_ref': size_kw / 1000.})

    @property
    def solar_multiple(self) -> float:
        raise NotImplementedError

    @solar_multiple.setter
    def solar_multiple(self, solar_multiple: float):
        raise NotImplementedError

    @property
    def tes_hours(self) -> float:
        return self.ssc.get('tshours')

    @tes_hours.setter
    def tes_hours(self, tes_hours: float):
        """
        Equivalent full-load thermal storage hours [hr]
        :param tes_hours:
        :return:
        """
        self.ssc.set({'tshours': tes_hours})

    @property
    def cycle_thermal_rating(self) -> float:
        raise NotImplementedError

    @property
    def field_thermal_rating(self) -> float:
        raise NotImplementedError

    @property
    def cycle_nominal_efficiency(self) -> float:
        raise NotImplementedError

    @property
    def number_of_reflector_units(self) -> float:
        raise NotImplementedError

    @property
    def minimum_receiver_power_fraction(self) -> float:
        raise NotImplementedError

    @property
    def field_tracking_power(self) -> float:
        raise NotImplementedError

    @property
    def htf_cold_design_temperature(self) -> float:
        """Returns cold design temperature for HTF [C]"""
        raise NotImplementedError

    @property
    def htf_hot_design_temperature(self) -> float:
        """Returns hot design temperature for HTF [C]"""
        raise NotImplementedError

    @property
    def initial_tes_hot_mass_fraction(self) -> float:
        """Returns initial thermal energy storage fraction of mass in hot tank [-]"""
        raise NotImplementedError

    #
    # Outputs
    #
    @property
    def dispatch(self):
        return self._dispatch

    @property
    def annual_energy_kw(self) -> float:
        if self.system_capacity_kw > 0:
            return sum(list(self.outputs.ssc_time_series['gen']))
        else:
            return 0

    @property
    def generation_profile(self) -> list:
        if self.system_capacity_kw:
            return list(self.outputs.ssc_time_series['gen'])
        else:
            return [0] * self.site.n_timesteps

    @property
    def capacity_factor(self) -> float:
        if self.system_capacity_kw > 0:
            return self.annual_energy_kw / self.system_capacity_kw * 8760
        else:
            return 0
