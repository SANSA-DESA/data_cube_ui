import datacube
from django.conf import settings
from utils.data_cube_utilities import data_access_api


class SansaDesaDataAccessApi(data_access_api.DataAccessApi):
    """Reimplementing in order to be able to pass a custom environment name

    Also, assume default config and env values as taken from the django settings
    module

    """

    def __init__(
            self,
            config=settings.DATACUBE_CONFIG_PATH,
            env=settings.DATACUBE_ENVIRONMENT
    ):
        self.dc = datacube.Datacube(config=config, env=env)