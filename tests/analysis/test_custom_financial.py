from pytest import approx, fixture
from pathlib import Path
from hybrid.sites import SiteInfo, flatirons_site
from hybrid.layout.hybrid_layout import PVGridParameters, WindBoundaryGridParameters
from hybrid.financial.custom_financial_model import CustomFinancialModel
from hybrid.hybrid_simulation import HybridSimulation
from hybrid.detailed_pv_plant import DetailedPVPlant
from examples.Detailed_PV_Layout.detailed_pv_layout import DetailedPVParameters, DetailedPVLayout
from hybrid.grid import Grid
import json


solar_resource_file = Path(__file__).absolute().parent.parent.parent / "resource_files" / "solar" / "35.2018863_-101.945027_psmv3_60_2012.csv"
wind_resource_file = Path(__file__).absolute().parent.parent.parent / "resource_files" / "wind" / "35.2018863_-101.945027_windtoolkit_2012_60min_80m_100m.srw"

@fixture
def site():
    return SiteInfo(flatirons_site, solar_resource_file=solar_resource_file, wind_resource_file=wind_resource_file)


default_fin_config = {
    'batt_replacement_schedule_percent': [0],
    'batt_bank_replacement': [0],
    'batt_replacement_option': 0,
    'batt_computed_bank_capacity': 0,
    'batt_meter_position': 0,
    'battery_per_kWh': 0,
    'en_batt': 0,
    'en_standalone_batt': 0,
    'om_fixed': [1],
    'om_production': [2],
    'om_capacity': (0,),
    'om_batt_fixed_cost': 0,
    'om_batt_variable_cost': [0],
    'om_batt_capacity_cost': 0,
    'om_batt_replacement_cost': 0,
    'om_replacement_cost_escal': 0,
    'system_use_lifetime_output': 0,
    'inflation_rate': 2.5,
    'real_discount_rate': 6.4,
    'cp_capacity_credit_percent': [0],

    # These are needed for hybrid_simulation, starting at "Tax Incentives"
    'ptc_fed_amount': [0],
    'ptc_fed_escal': 0,
    'itc_fed_amount': [0],
    'itc_fed_percent': [26],
    'depr_alloc_macrs_5_percent': 90,
    'depr_alloc_macrs_15_percent': 1.5,
    'depr_alloc_sl_5_percent': 0,
    'depr_alloc_sl_15_percent': 2.5,
    'depr_alloc_sl_20_percent': 3,
    'depr_alloc_sl_39_percent': 0,
    'depr_alloc_custom_percent': 0,
    'depr_bonus_fed_macrs_5': 1,
    'depr_bonus_sta_macrs_5': 1,
    'depr_itc_fed_macrs_5': 1,
    'depr_itc_sta_macrs_5': 1,
    'depr_bonus_fed_macrs_15': 1,
    'depr_bonus_sta_macrs_15': 1,
    'depr_itc_fed_macrs_15': 0,
    'depr_itc_sta_macrs_15': 0,
    'depr_bonus_fed_sl_5': 0,
    'depr_bonus_sta_sl_5': 0,
    'depr_itc_fed_sl_5': 0,
    'depr_itc_sta_sl_5': 0,
    'depr_bonus_fed_sl_15': 0,
    'depr_bonus_sta_sl_15': 0,
    'depr_itc_fed_sl_15': 0,
    'depr_itc_sta_sl_15': 0,
    'depr_bonus_fed_sl_20': 0,
    'depr_bonus_sta_sl_20': 0,
    'depr_itc_fed_sl_20': 0,
    'depr_itc_sta_sl_20': 0,
    'depr_bonus_fed_sl_39': 0,
    'depr_bonus_sta_sl_39': 0,
    'depr_itc_fed_sl_39': 0,
    'depr_itc_sta_sl_39': 0,
    'depr_bonus_fed_custom': 0,
    'depr_bonus_sta_custom': 0,
    'depr_itc_fed_custom': 0,
    'depr_itc_sta_custom': 0,

    'dc_degradation': 0,
    'ppa_soln_mode': 1,
}


def test_custom_financial(site):
    discount_rate = 0.0906       # [1/year]
    cash_flow = [
        -4.8274e+07, 3.57154e+07, 7.7538e+06, 4.76858e+06, 2.96768e+06,
        2.94339e+06, 1.5851e+06, 227235, 202615, 176816,
        149414, 120856, 90563.4, 58964.5, 25609.9,
        378270, 1.20607e+06, 633062, 3.19583e+06, 6.01239e+06,
        5.78599e+06, 5.53565e+06, 5.49998e+06, 5.4857e+06, 5.47012e+06,
        6.84512e+06]
    npv = CustomFinancialModel.npv(discount_rate, cash_flow)
    assert npv == approx(7412807, 1e-3)


def test_detailed_pv(site):
    # Run detailed PV model (pvsamv1) using a custom financial model
    annual_energy_expected = 108239401
    npv_expected = -45614395

    pvsamv1_defaults_file = Path(__file__).absolute().parent.parent / "hybrid/pvsamv1_basic_params.json"
    with open(pvsamv1_defaults_file, 'r') as f:
        tech_config = json.load(f)

    layout_params = PVGridParameters(x_position=0.5,
                                     y_position=0.5,
                                     aspect_power=0,
                                     gcr=0.3,
                                     s_buffer=2,
                                     x_buffer=2)
    interconnect_kw = 150e6


    detailed_pvplant = DetailedPVPlant(
        site=site,
        pv_config={
            'tech_config': tech_config,
            'layout_params': layout_params,
            'fin_model': CustomFinancialModel(default_fin_config),
        }
    )

    grid_source = Grid(
        site=site,
        grid_config={
            'interconnect_kw': interconnect_kw,
            'fin_model': CustomFinancialModel(default_fin_config),
        }
    )

    power_sources = {
        'pv': {
            'pv_plant': detailed_pvplant,
        },
        'grid': {
            'grid_source': grid_source
        }
    }
    hybrid_plant = HybridSimulation(power_sources, site)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.pv.dc_degradation = [0] * 25
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values
    assert aeps.pv == approx(annual_energy_expected, 1e-3)
    assert aeps.hybrid == approx(annual_energy_expected, 1e-3)
    assert npvs.pv == approx(npv_expected, 1e-3)
    assert npvs.hybrid == approx(npv_expected, 1e-3)


def test_hybrid_simple_pv_with_wind(site):
    # Run wind + simple PV (pvwattsv8) hybrid plant with custom financial model
    annual_energy_expected_pv = 98821626
    annual_energy_expected_wind = 33637984
    annual_energy_expected_hybrid = 132459610
    npv_expected_pv = -26845833
    npv_expected_wind = -13797953
    npv_expected_hybrid = -60608079

    interconnect_kw = 150e6
    pv_kw = 50000
    wind_kw = 10000

    grid_source = Grid(
        site=site,
        grid_config={
            'interconnect_kw': interconnect_kw,
            'fin_model': CustomFinancialModel(default_fin_config),
        }
    )

    power_sources = {
        'pv': {
            'system_capacity_kw': pv_kw,
            'layout_params': PVGridParameters(x_position=0.5,
                                              y_position=0.5,
                                              aspect_power=0,
                                              gcr=0.5,
                                              s_buffer=2,
                                              x_buffer=2),
            'fin_model': CustomFinancialModel(default_fin_config),
        },
        'wind': {
            'num_turbines': 5,
            'turbine_rating_kw': wind_kw / 5,
            'layout_mode': 'boundarygrid',
            'layout_params': WindBoundaryGridParameters(border_spacing=2,
                                                        border_offset=0.5,
                                                        grid_angle=0.5,
                                                        grid_aspect_power=0.5,
                                                        row_phase_offset=0.5),
            'fin_model': CustomFinancialModel(default_fin_config),
        },
        'grid': {
            'grid_source': grid_source,
        }
    }
    hybrid_plant = HybridSimulation(power_sources, site)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.pv.dc_degradation = [0] * 25
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values
    assert aeps.pv == approx(annual_energy_expected_pv, 1e-3)
    assert aeps.wind == approx(annual_energy_expected_wind, 1e-3)
    assert aeps.hybrid == approx(annual_energy_expected_hybrid, 1e-3)
    assert npvs.pv == approx(npv_expected_pv, 1e-3)
    assert npvs.wind == approx(npv_expected_wind, 1e-3)
    assert npvs.hybrid == approx(npv_expected_hybrid, 1e-3)


def test_hybrid_detailed_pv_with_wind(site):
    # Test wind + detailed PV (pvsamv1) hybrid plant with custom financial model
    annual_energy_expected_pv = 21500708
    annual_energy_expected_wind = 33637984
    annual_energy_expected_hybrid = 55138692
    npv_expected_pv = -9126126
    npv_expected_wind = -13797953
    npv_expected_hybrid = -22924079

    interconnect_kw = 150e6
    wind_kw = 10000

    pvsamv1_defaults_file = Path(__file__).absolute().parent.parent / "hybrid/pvsamv1_basic_params.json"
    with open(pvsamv1_defaults_file, 'r') as f:
        tech_config = json.load(f)
    
    # NOTE: PV array shrunk to avoid problem associated with flicker calculation
    tech_config['system_capacity'] = 10000
    tech_config['inverter_count'] = 10
    tech_config['subarray1_nstrings'] = 2687

    layout_params = PVGridParameters(x_position=0.5,
                                     y_position=0.5,
                                     aspect_power=0,
                                     gcr=0.3,
                                     s_buffer=2,
                                     x_buffer=2)

    detailed_pvplant = DetailedPVPlant(
        site=site,
        pv_config={
            'tech_config': tech_config,
            'layout_params': layout_params,
            'fin_model': CustomFinancialModel(default_fin_config),
        }
    )

    grid_source = Grid(
        site=site,
        grid_config={
            'interconnect_kw': interconnect_kw,
            'fin_model': CustomFinancialModel(default_fin_config),
        }
    )

    power_sources = {
        'pv': {
            'pv_plant': detailed_pvplant,
        },
        'wind': {
            'num_turbines': 5,
            'turbine_rating_kw': wind_kw / 5,
            'layout_mode': 'boundarygrid',
            'layout_params': WindBoundaryGridParameters(border_spacing=2,
                                                        border_offset=0.5,
                                                        grid_angle=0.5,
                                                        grid_aspect_power=0.5,
                                                        row_phase_offset=0.5),
            'fin_model': CustomFinancialModel(default_fin_config),
        },
        'grid': {
            'grid_source': grid_source,
        }
    }
    hybrid_plant = HybridSimulation(power_sources, site)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.pv.dc_degradation = [0] * 25
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values
    assert aeps.pv == approx(annual_energy_expected_pv, 1e-3)
    assert aeps.wind == approx(annual_energy_expected_wind, 1e-3)
    assert aeps.hybrid == approx(annual_energy_expected_hybrid, 1e-3)
    assert npvs.pv == approx(npv_expected_pv, 1e-3)
    assert npvs.wind == approx(npv_expected_wind, 1e-3)
    assert npvs.hybrid == approx(npv_expected_hybrid, 1e-3)
