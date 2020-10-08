"""Custom settings module for sansa-desa's data_cube_ui

We use this mostly to fetch additional settings from the environment

"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .settings import *


def get_env_variable(variable, default=None):
    prefix = 'DJANGO__'
    env_var_name = f'{prefix}{variable}'
    value = os.getenv(env_var_name)
    if value is None:
        if default:
            value = default
        else:
            raise ImproperlyConfigured(
                f'Add the {env_var_name!r} environment variable')
    return value


def get_bool_env_variable(variable, default=None):
    try:
        value = get_env_variable(variable, default=default)
    except ImproperlyConfigured:
        raise
    truthy_values = (
        True,
        '1',
        'on',
        'yes',
        'true',
    )
    return True if value.lower() in truthy_values else False


DEBUG = get_env_variable('DEBUG', False)

SECRET_KEY = get_env_variable('SECRET_KEY')

ADMIN_EMAIL = get_env_variable('ADMIN_EMAIL', ADMIN_EMAIL)

TIME_ZONE = 'UTC'

# not sure why the original data_cube_ui customized this yet
STATICFILES_DIRS = []

CELERY_BROKER_URL = get_env_variable(
    'CELERY_BROKER_URL', 'redis://localhost:6379')

CELERY_RESULT_BACKEND = get_env_variable(
    'CELERY_RESULT_BACKEND', CELERY_BROKER_URL)

DC_UI_DIR = str(Path(BASE_DIR) / 'utils')