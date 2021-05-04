import pytest

from hybrid.sites import SiteInfo, flatirons_site
from hybrid.layout.hybrid_layout import WindBoundaryGridParameters, SolarGridParameters
from hybrid.hybrid_simulation import HybridSimulation


@pytest.fixture
def site():
    return SiteInfo(flatirons_site)


interconnection_size_kw = 15000
technologies = {'solar': {
                    'system_capacity_kw': 5000,
                    'layout_params': SolarGridParameters(x_position=0.5,
                                                         y_position=0.5,
                                                         aspect_power=0,
                                                         gcr=0.5,
                                                         s_buffer=2,
                                                         x_buffer=2)
                },
                'wind': {
                    'num_turbines': 5,
                    'turbine_rating_kw': 2000,
                    'layout_mode': 'boundarygrid',
                    'layout_params': WindBoundaryGridParameters(border_spacing=2,
                                                                border_offset=0.5,
                                                                grid_angle=0.5,
                                                                grid_aspect_power=0.5,
                                                                row_phase_offset=0.5)
                },
                'battery': 20 * 1000,
                'grid': 15000}


def test_hybrid_wind_only(site):
    wind_only = {key: technologies[key] for key in ('wind', 'grid')}
    hybrid_plant = HybridSimulation(wind_only, site, interconnect_kw=interconnection_size_kw)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values

    assert aeps.solar == 0
    assert aeps.wind == pytest.approx(33615479.57, 1e-3)
    assert aeps.hybrid == pytest.approx(33615479.57, 1e-3)

    assert npvs.solar == 0
    assert npvs.wind == pytest.approx(-26787334.05, 1e-3)
    assert npvs.hybrid == pytest.approx(-26787334.05, 1e-3)


def test_hybrid_solar_only(site):
    solar_only = {key: technologies[key] for key in ('solar', 'grid')}
    hybrid_plant = HybridSimulation(solar_only, site, interconnect_kw=interconnection_size_kw)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values

    assert aeps.solar == pytest.approx(8703525.94, 1e-3)
    assert aeps.wind == 0
    assert aeps.hybrid == pytest.approx(8703525.94, 1e-3)

    assert npvs.solar == pytest.approx(-8726996.89, 1e-3)
    assert npvs.wind == 0
    assert npvs.hybrid == pytest.approx(-8726996.89, 1e-3)


def test_hybrid_(site):
    """
    Performance from Wind is slightly different from wind-only case because the solar presence modified the wind layout
    """
    solar_wind_hybrid = {key: technologies[key] for key in ('solar', 'wind', 'grid')}
    hybrid_plant = HybridSimulation(solar_wind_hybrid, site, interconnect_kw=interconnection_size_kw)
    hybrid_plant.layout.plot()
    hybrid_plant.ppa_price = (0.01, )
    hybrid_plant.simulate()
    # plt.show()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values

    assert aeps.solar == pytest.approx(8703525.94, 1e-3)
    assert aeps.wind == pytest.approx(32978136.69, 1e-3)
    assert aeps.hybrid == pytest.approx(41681662.63, 1e-3)

    assert npvs.solar == pytest.approx(-8726996.89, 1e-3)
    assert npvs.wind == pytest.approx(-26915537.23, 1e-3)
    assert npvs.hybrid == pytest.approx(-36225522.87, 1e-3)


def test_hybrid_with_storage_dispatch(site):
    hybrid_plant = HybridSimulation(technologies, site, interconnect_kw=interconnection_size_kw)
    hybrid_plant.ppa_price = (0.03, )
    hybrid_plant.simulate()
    aeps = hybrid_plant.annual_energies
    npvs = hybrid_plant.net_present_values

    print(aeps)
    print(npvs)

    assert aeps.solar == pytest.approx(8703525.938, 1e-3)
    assert aeps.wind == pytest.approx(33615479.573, 1e-3)
    assert aeps.battery == pytest.approx(-219275.341, 1e-3)
    assert aeps.hybrid == pytest.approx(42099730.169, 1e-3)

    assert npvs.solar == pytest.approx(-3557478.980, 1e-3)
    assert npvs.wind == pytest.approx(-7486447.180, 1e-3)
    assert npvs.battery == pytest.approx(0, 1e-3)
    assert npvs.hybrid == pytest.approx(-9558405.273, 1e-3)
