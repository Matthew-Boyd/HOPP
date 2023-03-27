from dataclasses import dataclass, _is_classvar, asdict
from typing import Sequence, List
import numpy as np


@dataclass
class FinancialData:
    """
    Groups similar variables together into logical organization and replicates some of PySAM.Singleowner's subclass structure
    HybridSimulation has some financial-model-required properties that are stored within subclasses
    These are accessed in the following ways: 
        ```
        hybrid_simulation_model.VariableGroup.variable_name
        hybrid_simulation_model.VariableGroup.export()
        ```
    This dataclass duplicates that structure for a custom financial model so that it can be interoperable
    within HybridSimulation
    """
    def items(self):
        return asdict(self).items()

    def __getitem__(self, item):
        return getattr(self, item)

    def export(self):
        return asdict(self)

    def assign(self, input_dict):
        for k, v in input_dict.items:
            if hasattr(self, k):
                setattr(self[k], v)
            else:
                raise IOError(f"{self.__class__}'s attribute {v} does not exist.")

    @classmethod
    def from_dict(cls, input_dict):
        fields = cls.__dataclass_fields__
        # parse the input dict, but only include keys that are:        
        return cls(**{
            k: v for k, v in input_dict.items()                  
            if k in fields                                # - part of the fields
            and fields[k].init                            # - not a post_init field
        })


@dataclass
class BatterySystem(FinancialData):
    """
    These financial inputs are used in simulate_financials in `battery.py`
    The names correspond to PySAM.Singleowner variables.
    To add any additional system cost, first see if the variable exists in Singleowner, and re-use name.
    This will simplify interoperability
    """
    batt_bank_replacement: tuple
    batt_computed_bank_capacity: tuple
    batt_meter_position: tuple
    batt_replacement_option: float
    batt_replacement_schedule_percent: tuple
    battery_per_kWh: float
    en_batt: float
    en_standalone_batt: float


@dataclass
class SystemCosts(FinancialData):
    om_fixed: tuple
    om_production: tuple
    om_capacity: tuple
    om_batt_fixed_cost: float
    om_batt_variable_cost: float
    om_batt_capacity_cost: float
    om_batt_replacement_cost: float
    om_replacement_cost_escal: float
    total_installed_cost: float=None


@dataclass
class Revenue(FinancialData):
    ppa_price_input: float=None
    ppa_soln_mode: float=1
    ppa_escalation: float=0
    ppa_multiplier_model: float=None
    dispatch_factors_ts: Sequence=(0,)


@dataclass
class FinancialParameters(FinancialData):
    construction_financing_cost: float=None
    analysis_period: float=None
    inflation_rate: float=None
    real_discount_rate: float=None


@dataclass
class Outputs(FinancialData):
    """
    These financial outputs are all matched with PySAM.Singleowner outputs, but most have different names.
    For example, `net_present_value` is `Singleowner.project_return_aftertax_npv`.
    To see the PySAM variable referenced by each name below, see power_source.py's Output section.
    Any additional financial outputs should be added here. 
    The names can be different from the PySAM.Singleowner names.
    To enable programmatic access via the HybridSimulation class, getter and setters can be added
    """
    cp_capacity_payment_amount: float=None
    capacity_factor: float=None
    net_present_value: float=None
    cost_installed: float=None
    internal_rate_of_return: float=None
    energy_sales_value: float=None
    energy_purchases_value: float=None
    energy_value: float=None
    federal_depreciation_total: float=None
    federal_taxes: float=None
    debt_payment: float=None
    insurance_expense: float=None
    tax_incentives: float=None
    om_capacity_expense: float=None
    om_fixed_expense: float=None
    om_variable_expense: float=None
    om_total_expense: float=None
    levelized_cost_of_energy_real: float=None
    levelized_cost_of_energy_nominal: float=None
    total_revenue: float=None
    capacity_payment: float=None
    benefit_cost_ratio: float=None
    project_return_aftertax_npv: float=None
    cf_project_return_aftertax: Sequence=(0,)


@dataclass
class SystemOutput(FinancialData):
    gen: Sequence=(0,)
    system_capacity: float=None
    degradation: Sequence=(0,)
    system_pre_curtailment_kwac: float=None
    annual_energy_pre_curtailment_ac: float=None


class CustomFinancialModel():
    """
    This custom financial model slots into the PowerSource's financial model that is originally a PySAM.Singleowner model
    PowerSource and the overlaying classes that call on PowerSource expect properties and functions from the financial model
    The mininum expectations are listed here as the class interface.
    
    The financial model is constructed with financial configuration inputs.
    During simulation, the financial model needs to update all its design inputs from changes made to
    the system performance models, such as changing capacities, total_installed_cost, benefits, etc.
    Part of this is done in HybridSimulation::calculate_financials, which uses many more of PySAM.Singleowner
    inputs than are included here. Any of those variables can be added here.
    
    This class can be expanded with completely new variables too, which can be added to the class itself or within a dataclass.
    Any financial variable's dependence on system design needs to be accounted for.

    :param fin_config: dictionary of financial parameters
    """
    def __init__(self,
                 fin_config: dict) -> None:

        # Input parameters
        self.project_life = None
        self.system_use_lifetime_output = None          # Lifetime
        self.cp_capacity_credit_percent = None          # CapacityPayments

        # Input parameters within dataclasses
        self.BatterySystem: BatterySystem = BatterySystem.from_dict(fin_config)
        self.SystemCosts: SystemCosts = SystemCosts.from_dict(fin_config)
        self.Revenue: Revenue = Revenue.from_dict(fin_config)
        self.FinancialParameters: FinancialParameters = FinancialParameters.from_dict(fin_config)
        self.SystemOutput: SystemOutput = SystemOutput()
        self.Outputs: Outputs = Outputs()
        self.subclasses = [self.BatterySystem, self.SystemCosts,
                           self.Revenue, self.FinancialParameters,
                           self.SystemOutput, self.Outputs]
        self.assign(fin_config)


    def set_financial_inputs(self, power_source_dict: dict):
        """
        Set financial inputs from PowerSource (e.g., PVPlant) parameters and outputs
        """
        if 'project_life' in power_source_dict:
            self.value('project_life', power_source_dict['project_life'])
        if 'system_capacity' in power_source_dict:
            self.value('system_capacity', power_source_dict['system_capacity'])
        if 'dc_degradation' in power_source_dict:
            self.value('degradation', power_source_dict['dc_degradation'])


    def execute(self, n=0):
        npv = self.npv(
                rate=self.nominal_discount_rate(
                    inflation_rate=self.value('inflation_rate'),
                    real_discount_rate=self.value('real_discount_rate')
                    ),
                net_cash_flow=self.net_cash_flow(self.project_life)
                )
        self.value('project_return_aftertax_npv', npv)
        return


    @staticmethod
    def npv(rate: float, net_cash_flow: List[float]):
        """
        Returns the NPV (Net Present Value) of a cash flow series.

        borrowed from the numpy-financial package
        """
        values = np.atleast_2d(net_cash_flow)
        timestep_array = np.arange(0, values.shape[1])
        npv = (values / (1 + rate) ** timestep_array).sum(axis=1)
        try:
            # If size of array is one, return scalar
            return npv.item()
        except ValueError:
            # Otherwise, return entire array
            return npv


    @staticmethod
    def nominal_discount_rate(inflation_rate: float, real_discount_rate: float):
        """
        Computes the nominal discount rate [%]

        :param inflation_rate: inflation rate [%]
        :param real_discount_rate: real discount rate [%]
        """
        if inflation_rate is None:
            raise Exception("'inflation_rate' must be a number.")
        if real_discount_rate is None:
            raise Exception("'real_discount_rate' must be a number.")

        return ( (1 + real_discount_rate / 100) * (1 + inflation_rate / 100) - 1 ) * 100


    def net_cash_flow(self, project_life=25):
        """
        Computes the net cash flow timeseries of annual values over lifetime
        """
        degradation = self.value('degradation')
        if isinstance(degradation, float) or isinstance(degradation, int):
            degradation = [degradation] * project_life
        elif len(degradation) == 1:
            degradation = [degradation[0]] * project_life
        else:
            degradation = list(degradation)

        ncf = list()
        ncf.append(-self.value('total_installed_cost'))
        degrad_fraction = 1                         # fraction of annual energy after degradation
        for year in range(1, project_life + 1):
            degrad_fraction *= (1 - degradation[year - 1])
            ncf.append(
                        (
                        - self.o_and_m_cost() * (1 + self.value('inflation_rate') / 100)**(year - 1)
                        + self.value('annual_energy')
                        * degrad_fraction
                        * self.value('ppa_price_input')[0]
                        * (1 + self.value('ppa_escalation') / 100)**(year - 1)
                        )
                      )
        return ncf


    def o_and_m_cost(self):
        """
        Computes the annual O&M cost from the fixed, per capacity and per production costs
        """
        return self.value('om_fixed')[0] \
               + self.value('om_capacity')[0] * self.value('system_capacity') \
               + self.value('om_production')[0] * self.value('annual_energy') * 1e-3


    def value(self, var_name, var_value=None):
        # TODO: is this the best way to replicate this functionality within the PySAM value() function,
        # where (array([15.]),) becomes (15,), like in set_average_for_hybrid("om_capacity", size_ratios) ?
        if (isinstance(var_value, tuple) or isinstance(var_value, list)) and isinstance(var_value[0], np.ndarray):
            var_value = (float(var_value[0][0]),)
        elif isinstance(var_value, np.ndarray) and isinstance(var_value[0], np.float64):
            var_value = float(var_value[0])  # used for aggregate NPV value

        attr_obj = None
        if var_name in self.__dir__():
            attr_obj = self
        if not attr_obj:
            for sc in self.subclasses:
                if var_name in sc.__dir__():
                    attr_obj = sc
                    break
        if not attr_obj:
            raise ValueError("Variable {} not found in CustomFinancialModel".format(var_name))

        if var_value is None:
            try:
                return getattr(attr_obj, var_name)
            except Exception as e:
                raise IOError(f"{self.__class__}'s attribute {var_name} error: {e}")
        else:
            try:
                setattr(attr_obj, var_name, var_value)
            except Exception as e:
                raise IOError(f"{self.__class__}'s attribute {var_name} could not be set to {var_value}: {e}")

    
    def assign(self, input_dict):
        for k, v in input_dict.items():
            if not isinstance(v, dict):
                self.value(k, v)
            else:
                getattr(self, k).assign(v)


    def export_battery_values(self):
        return {
            'batt_bank_replacement': self.BatterySystem.batt_bank_replacement,
            'batt_computed_bank_capacity': self.BatterySystem.batt_computed_bank_capacity,
            'batt_meter_position': self.BatterySystem.batt_meter_position,
            'batt_replacement_option': self.BatterySystem.batt_replacement_option,
            'batt_replacement_schedule_percent': self.BatterySystem.batt_replacement_schedule_percent,
            'battery_per_kWh': self.BatterySystem.battery_per_kWh,
            'en_batt': self.BatterySystem.en_batt,
            'en_standalone_batt': self.BatterySystem.en_standalone_batt,
        }


    @property
    def annual_energy(self) -> float:
        return self.value('annual_energy_pre_curtailment_ac')
    