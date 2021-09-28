from typing import Optional, Union, Sequence
import PySAM.TroughPhysical as Trough
import PySAM.Singleowner as Singleowner

from hybrid.power_source import *







class TroughPlant(PowerSource):
    _system_model: Union[Windpower.Windpower, Floris]
    _financial_model: Singleowner.Singleowner
    # _layout: TroughLayout
    # _dispatch: TroughDispatch

    def __init__(self,
                 site: SiteInfo,
                 trough_config: dict):
        system_model = Trough.default('PhysicalTroughSingleOwner')
        financial_model = Singleowner.from_existing(system_model, 'PhysicalTroughSingleOwner')

        super().__init__("SolarPlant", site, system_model, financial_model)

        self._system_model.SolarResource.solar_resource_data = self.site.solar_resource.data

        