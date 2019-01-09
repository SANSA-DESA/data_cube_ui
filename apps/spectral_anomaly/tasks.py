from celery.task import task
from celery import chain, group, chord
from datetime import datetime, timedelta
import shutil
import xarray as xr
import numpy as np
from xarray.ufuncs import logical_or as xr_or
import os

from utils.data_cube_utilities.data_access_api import DataAccessApi
from utils.data_cube_utilities.dc_utilities import (create_cfmask_clean_mask, create_bit_mask, write_geotiff_from_xr,
                                                    write_png_from_xr, add_timestamp_data_to_xr, clear_attrs)
from utils.data_cube_utilities.dc_chunker import (create_geographic_chunks, create_time_chunks,
                                                  combine_geographic_chunks)
from apps.dc_algorithm.utils import create_2d_plot

from .models import SpectralAnomalyTask
from apps.dc_algorithm.models import Satellite
from apps.dc_algorithm.tasks import DCAlgorithmBase

import matplotlib.pyplot as plt
import matplotlib as mpl

from utils.data_cube_utilities.dc_ndvi_anomaly import NDVI, EVI
from utils.data_cube_utilities.dc_water_classifier import wofs_classify
from utils.data_cube_utilities.urbanization import NDBI
from utils.data_cube_utilities.dc_fractional_coverage_classifier import frac_coverage_classify
spectral_indices_function_map = {
    'ndvi': NDVI, 'ndwi': wofs_classify,
    'ndbi': NDBI, 'evi': EVI,
    'fractional_cover': frac_coverage_classify
}
spectral_indices_range_map = {
    'ndvi': (-1, 1), 'ndwi': (0, 1),
    'ndbi': (-1, 1), 'evi': (-1, 1),
    'fractional_cover': (-1, 1) # TODO: What is the range for fractional cover?
}

from celery.utils.log import get_task_logger
logger = get_task_logger(__name__)

class BaseTask(DCAlgorithmBase):
    app_name = 'spectral_anomaly'


@task(name="spectral_anomaly.run", base=BaseTask)
def run(task_id=None):
    """Responsible for launching task processing using celery asynchronous processes

    Chains the parsing of parameters, validation, chunking, and the start to data processing.
    """
    chain(
        parse_parameters_from_task.s(task_id=task_id),
        validate_parameters.s(task_id=task_id),
        perform_task_chunking.s(task_id=task_id),
        start_chunk_processing.s(task_id=task_id))()
    return True


@task(name="spectral_anomaly.parse_parameters_from_task", base=BaseTask)
def parse_parameters_from_task(task_id=None):
    """Parse out required DC parameters from the task model.

    See the DataAccessApi docstrings for more information.
    Parses out platforms, products, etc. to be used with DataAccessApi calls.

    If this is a multisensor app, platform and product should be pluralized and used
    with the get_stacked_datasets_by_extent call rather than the normal get.

    Returns:
        parameter dict with all keyword args required to load data.

    """
    task = SpectralAnomalyTask.objects.get(pk=task_id)

    parameters = {
        'platform': task.satellite.datacube_platform,
        'product': task.satellite.get_product(task.area_id),
        'time': (task.time_start, task.time_end),
        'baseline_time': (task.baseline_time_start, task.baseline_time_end),
        'analysis_time': (task.analysis_time_start, task.analysis_time_end),
        'longitude': (task.longitude_min, task.longitude_max),
        'latitude': (task.latitude_min, task.latitude_max),
        'measurements': task.satellite.get_measurements(),
        'composite_range': (task.composite_threshold_min, task.composite_threshold_max),
        'change_range': (task.change_threshold_min, task.change_threshold_max),
    }

    logger.info("parameters: {}".format(parameters))

    task.execution_start = datetime.now()
    task.update_status("WAIT", "Parsed out parameters.")

    return parameters


@task(name="spectral_anomaly.validate_parameters", base=BaseTask)
def validate_parameters(parameters, task_id=None):
    """Validate parameters generated by the parameter parsing task

    All validation should be done here - are there data restrictions?
    Combinations that aren't allowed? etc.

    Returns:
        parameter dict with all keyword args required to load data.
        -or-
        updates the task with ERROR and a message, returning None

    """
    logger.info("parameters validate_parameters: {}".format(parameters))
    task = SpectralAnomalyTask.objects.get(pk=task_id)
    dc = DataAccessApi(config=task.config_path)

    baseline_parameters = parameters.copy()
    baseline_parameters['time'] = parameters['baseline_time']
    baseline_acquisitions = dc.list_acquisition_dates(**baseline_parameters)

    analysis_parameters = parameters.copy()
    analysis_parameters['time'] = parameters['analysis_time']
    analysis_acquisitions = dc.list_acquisition_dates(**analysis_parameters)

    logger.info("baseline_acquisitions: {}".format(baseline_acquisitions))
    logger.info("analysis_acquisitions: {}".format(analysis_acquisitions))

    if len(baseline_acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquisitions for this parameter set "
                                    "for the baseline time period.")
        return None

    if len(analysis_acquisitions) < 1:
        task.complete = True
        task.update_status("ERROR", "There are no acquisitions for this parameter set "
                                    "for the analysis time period.")
        return None

    task.update_status("WAIT", "Validated parameters.")

    if not dc.validate_measurements(parameters['product'], parameters['measurements']):
        task.complete = True
        task.update_status(
            "ERROR",
            "The provided Satellite model measurements aren't valid for the product. Please check the measurements listed in the {} model.".
            format(task.satellite.name))
        return None

    dc.close()
    return parameters


@task(name="spectral_anomaly.perform_task_chunking", base=BaseTask)
def perform_task_chunking(parameters, task_id=None):
    """Chunk parameter sets into more manageable sizes

    Uses functions provided by the task model to create a group of
    parameter sets that make up the arg.

    Args:
        parameters: parameter stream containing all kwargs to load data

    Returns:
        parameters with a list of geographic and time ranges
    """
    if parameters is None:
        return None
    logger.info("parameters perform_task_chunking: {}".format(parameters))
    task = SpectralAnomalyTask.objects.get(pk=task_id)
    dc = DataAccessApi(config=task.config_path)
    dates = dc.list_acquisition_dates(**parameters)
    task_chunk_sizing = task.get_chunk_size()

    geographic_chunks = create_geographic_chunks(
        longitude=parameters['longitude'],
        latitude=parameters['latitude'],
        geographic_chunk_size=task_chunk_sizing['geographic'])

    time_chunks = create_time_chunks(
        dates, _reversed=task.get_reverse_time(), time_chunk_size=task_chunk_sizing['time'])
    logger.info("Time chunks: {}, Geo chunks: {}".format(len(time_chunks), len(geographic_chunks)))

    logger.info("geographic_chunks: {}".format(geographic_chunks))
    logger.info("time_chunks: {}".format(time_chunks))

    dc.close()
    task.update_status("WAIT", "Chunked parameter set.")
    return {'parameters': parameters, 'geographic_chunks': geographic_chunks, 'time_chunks': time_chunks}


@task(name="spectral_anomaly.start_chunk_processing", base=BaseTask)
def start_chunk_processing(chunk_details, task_id=None):
    """Create a fully asyncrhonous processing pipeline from paramters and a list of chunks.

    The most efficient way to do this is to create a group of time chunks for each geographic chunk,
    recombine over the time index, then combine geographic last.
    If we create an animation, this needs to be reversed - e.g. group of geographic for each time,
    recombine over geographic, then recombine time last.

    The full processing pipeline is completed, then the create_output_products task is triggered, completing the task.
    """
    if chunk_details is None:
        return None

    parameters = chunk_details.get('parameters')
    logger.info("parameters start_chunk_processing: {}".format(parameters))
    geographic_chunks = chunk_details.get('geographic_chunks')
    time_chunks = chunk_details.get('time_chunks')

    task = SpectralAnomalyTask.objects.get(pk=task_id)
    task.total_scenes = len(geographic_chunks) * len(time_chunks) * (task.get_chunk_size()['time']
                                                                     if task.get_chunk_size()['time'] is not None else
                                                                     len(time_chunks[0]))
    task.scenes_processed = 0
    task.update_status("WAIT", "Starting processing.")

    logger.info("START_CHUNK_PROCESSING")

    processing_pipeline = group([
        group([
            processing_task.s(
                task_id=task_id,
                geo_chunk_id=geo_index,
                time_chunk_id=time_index,
                geographic_chunk=geographic_chunk,
                time_chunk=time_chunk,
                **parameters) for time_index, time_chunk in enumerate(time_chunks)
        ]) for geo_index, geographic_chunk in enumerate(geographic_chunks)
    ]) | recombine_geographic_chunks.s(task_id=task_id)

    processing_pipeline = (processing_pipeline | create_output_products.s(task_id=task_id)).apply_async()
    return True


@task(name="spectral_anomaly.processing_task", acks_late=True, base=BaseTask)
def processing_task(task_id=None,
                    geo_chunk_id=None,
                    time_chunk_id=None,
                    geographic_chunk=None,
                    time_chunk=None,
                    **parameters):
    """Process a parameter set and save the results to disk.

    Uses the geographic and time chunk id to identify output products.
    **params is updated with time and geographic ranges then used to load data.
    the task model holds the iterative property that signifies whether the algorithm
    is iterative or if all data needs to be loaded at once.

    Args:
        task_id, geo_chunk_id, time_chunk_id: identification for the main task and what chunk this is processing
        geographic_chunk: range of latitude and longitude to load - dict with keys latitude, longitude
        time_chunk: list of acquisition dates
        parameters: all required kwargs to load data.

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    logger.info("parameters processing_task: {}".format(parameters))
    chunk_id = "_".join([str(geo_chunk_id), str(time_chunk_id)])
    task = SpectralAnomalyTask.objects.get(pk=task_id)

    logger.info("Starting chunk: " + chunk_id)
    if not os.path.exists(task.get_temp_path()):
        return None

    metadata = {}

    # For both the baseline and analysis time ranges for this
    # geographic chunk, load, calculate the spectral index, composite,
    # and filter the data according to user-supplied parameters -
    # recording where the data was out of the filter's range so we can
    # create the output product (an image).
    logger.info('geographic_chunk: {}'.format(geographic_chunk))
    logger.info('time_chunk: {}'.format(time_chunk))
    logger.info('task.baseline_time_start: {}'.format(task.baseline_time_start))
    dc = DataAccessApi(config=task.config_path)
    updated_params = parameters
    updated_params.update(geographic_chunk)
    logger.info("parameters: {}".format(parameters))
    logger.info("updated_params: {}".format(updated_params))
    spectral_index = task.query_type.result_id
    logger.info("spectral_index: {}".format(spectral_index))
    composites = {}
    composites_out_of_range = {}
    for composite_name in ['baseline', 'analysis']:
        # Use the corresponding time range for the baseline and analysis data.
        updated_params['time'] = \
            updated_params['baseline_time' if composite_name == 'baseline' else 'analysis_time']
        logger.info("composite_name: {}".format(composite_name))
        time_column_data = dc.get_dataset_by_extent(**updated_params)
        time_column_data[spectral_index] = spectral_indices_function_map[spectral_index](time_column_data)
        time_column_clean_mask = task.satellite.get_clean_mask_func()(time_column_data)
        logger.info("time_column_clean_mask: {}".format(time_column_clean_mask))
        # Drop unneeded data variables.
        measurements_list = task.satellite.measurements.replace(" ", "").split(",")
        # logger.info("measurements_list: {}".format(measurements_list))
        time_column_data = time_column_data.drop(measurements_list)
        logger.info("time_column_data: {}".format(time_column_data))
        metadata = task.metadata_from_dataset(metadata, time_column_data,
                                              time_column_clean_mask, parameters)
        # Obtain the composite.
        composite = task.get_processing_method()(time_column_data,
                                                 clean_mask=time_column_clean_mask,
                                                 no_data=task.satellite.no_data_value)
        composites[composite_name] = composite
        # Determine where the composite is out of range.
        composites_out_of_range[composite_name] = \
            xr_or(composite[spectral_index] < task.composite_threshold_min,
                  task.composite_threshold_max < composite[spectral_index])
        logger.info("composite_out_of_range: {}".format(composites_out_of_range[composite_name]))
    dc.close()
    # Create a difference composite.
    diff_composite = composites['analysis'] - composites['baseline']
    logger.info("diff_composite: {}".format(diff_composite))
    # Find where either the baseline or analysis composite was out of range for a pixel.
    composite_out_of_range = xr_or(*composites_out_of_range.values())
    logger.info("composite_out_of_range: {}".format(composite_out_of_range))
    # Find where either the baseline or analysis composite was no_data.
    composite_no_data = \
        xr_or(composites['baseline'][spectral_index] == task.satellite.no_data_value,
              composites['analysis'][spectral_index] == task.satellite.no_data_value)

    composite_path = os.path.join(task.get_temp_path(), chunk_id + ".nc")
    diff_composite.to_netcdf(composite_path)
    composite_out_of_range_path = os.path.join(task.get_temp_path(), chunk_id + "_out_of_range.nc")
    composite_out_of_range.to_netcdf(composite_out_of_range_path)
    composite_no_data_path = os.path.join(task.get_temp_path(), chunk_id + "_no_data.nc")
    composite_no_data.to_netcdf(composite_no_data_path)
    logger.info("Done with chunk: " + chunk_id)
    return composite_path, composite_out_of_range_path, composite_no_data_path, metadata, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="spectral_anomaly.recombine_geographic_chunks", base=BaseTask)
def recombine_geographic_chunks(chunks, task_id=None):
    """Recombine processed data over the geographic indices

    For each geographic chunk process spawned by the main task, open the resulting dataset
    and combine it into a single dataset. Combine metadata as well, writing to disk.

    Args:
        chunks: list of the return from the processing_task function - path, metadata, and {chunk ids}

    Returns:
        path to the output product, metadata dict, and a dict containing the geo/time ids
    """
    logger.info("RECOMBINE_GEO")
    total_chunks = [chunks] if not isinstance(chunks, list) else chunks
    total_chunks = [chunk for chunk in total_chunks if chunk is not None]

    metadata = {}
    task = SpectralAnomalyTask.objects.get(pk=task_id)

    composite_chunk_data = []
    out_of_range_chunk_data = []
    no_data_chunk_data = []

    for index, chunk in enumerate(total_chunks):
        metadata = task.combine_metadata(metadata, chunk[3])
        composite_chunk_data.append(xr.open_dataset(chunk[0], autoclose=True))
        out_of_range_chunk_data.append(xr.open_dataset(chunk[1], autoclose=True))
        no_data_chunk_data.append(xr.open_dataset(chunk[2], autoclose=True))

    combined_composite_data = combine_geographic_chunks(composite_chunk_data)
    combined_out_of_range_data = combine_geographic_chunks(out_of_range_chunk_data)
    combined_no_data = combine_geographic_chunks(no_data_chunk_data)

    logger.info("combined_composite_data: {}".format(combined_composite_data))
    logger.info("combined_out_of_range_data: {}".format(combined_out_of_range_data))
    logger.info("combined_no_data: {}".format(combined_no_data))

    composite_path = os.path.join(task.get_temp_path(), "full_composite.nc")
    combined_composite_data.to_netcdf(composite_path)
    composite_out_of_range_path = os.path.join(task.get_temp_path(), "full_composite_out_of_range.nc")
    combined_out_of_range_data.to_netcdf(composite_out_of_range_path)
    no_data_path = os.path.join(task.get_temp_path(), "full_composite_no_data.nc")
    combined_no_data.to_netcdf(no_data_path)
    # logger.info("Done combining geographic chunks for time: " + str(time_chunk_id))
    logger.info("Done combining geographic chunks.")
    return composite_path, composite_out_of_range_path, no_data_path, metadata#, {'geo_chunk_id': geo_chunk_id, 'time_chunk_id': time_chunk_id}


@task(name="spectral_anomaly.create_output_products", base=BaseTask)
def create_output_products(data, task_id=None):
    """Create the final output products for this algorithm.

    Open the final dataset and metadata and generate all remaining metadata.
    Convert and write the dataset to variuos formats and register all values in the task model
    Update status and exit.

    Args:
        diff_data: tuple in the format of processing_task function - path, metadata, and {chunk ids}

    """
    logger.info("CREATE_OUTPUT")
    # logger.info("create_ouput_products - data: {}".format(data))
    full_metadata = data[3]
    logger.info("full_metadata: {}".format(full_metadata))
    task = SpectralAnomalyTask.objects.get(pk=task_id)
    spectral_index = task.query_type.result_id

    # This is the difference (or "change") composite.
    diff_composite = xr.open_dataset(data[0], autoclose=True)
    # This indicates where either the baseline or analysis composite
    # was outside the corresponding user-specified range.
    orig_composite_out_of_range = xr.open_dataset(data[1], autoclose=True)\
                                  [spectral_index].astype(np.bool).values
    logger.info("orig_composite_out_of_range: {}".format(orig_composite_out_of_range))
    logger.info("orig_composite_out_of_range sum, size: {} {}"\
                .format(orig_composite_out_of_range.sum(), orig_composite_out_of_range.size))
    # This indicates where either the baseline or analysis composite
    # was the no_data value.
    composite_no_data = xr.open_dataset(data[2], autoclose=True)\
                        [spectral_index].astype(np.bool).values
    logger.info("composite_no_data: {}".format(composite_no_data))

    task.result_path = os.path.join(task.get_result_path(), "png_mosaic.png")
    task.data_path = os.path.join(task.get_result_path(), "data_tif.tif")
    task.final_metadata_from_dataset(diff_composite)
    task.metadata_from_dict(full_metadata)

    # 1. Save the spectral index net change as a GeoTIFF.
    write_geotiff_from_xr(task.data_path, diff_composite.astype('float32'), bands=[spectral_index], no_data=task.satellite.no_data_value)
    # TODO: 2. Create a PNG of the spectral index change composite.
    # 2.1. Find the min and max possible difference for the selected spectral index.
    spec_ind_min, spec_ind_max = spectral_indices_range_map[spectral_index]
    diff_min_possible, diff_max_possible = spec_ind_min - spec_ind_max, spec_ind_max - spec_ind_min
    # im = plt.imshow(composite[spectral_index].values, vmin=vmin, vmax=vmax)
    # plt.imsave(task.result_path, composite[spectral_index].values, vmin=vmin, vmax=vmax)

    diff_data = diff_composite[spectral_index].values
    logger.info("diff_data min/mean/max: {}, {}, {}"
                .format(diff_data.min(), diff_data.mean(), diff_data.max()))
    logger.info("Number of NaN elements in diff_data: {}"
                .format(np.sum(np.isnan(diff_data))))
    logger.info("Number of nodata elements in diff_data: {}"
                .format(np.sum(diff_data == task.satellite.no_data_value)))
    # Mask out the no-diff_data values.
    # data_no_data_masked = np.ma.array(diff_data, mask=(diff_data==task.satellite.no_data_value))
    # no_data_mask = diff_data==task.satellite.no_data_value
    # logger.info("After masking no_data values, diff_data: {}"
    #             .format(data_no_data_masked.min(), data_no_data_masked.max()))
    # 2.2. Scale the difference composite to the range [0, 1] for plotting.
    image_data = np.interp(diff_data, (diff_min_possible, diff_max_possible), (0, 1))
    logger.info("type(image_data): {}".format(type(image_data)))
    logger.info("image_data (after np.interp()): {}".format(image_data))
    logger.info("image_data min/mean/max: {}, {}, {}"
                .format(image_data.min(), image_data.mean(), image_data.max()))
    # image_data = np.empty((*composite.shape, 3), dtype=np.uint8)
    # TODO: Scale the difference composite to the range [-1, 1] so the optional
    # TODO: user-specified change value range must always be within [-1, 1].
    # TODO: Without this, the bounds are dependent on the spectral index range -
    # TODO: more specifically, the bounds are (min-max, max-min) for a given spectral index.
    # TODO: For example, without this the bounds on the user-specified change value
    # TODO: range for NDVI is [-2, 2].
    # diff_data = np.interp(diff_data, (diff_min_possible, diff_max_possible), (-1, 1))
    # 2.3. Color by region.
    # 2.3.1. First, color by change with a red-green gradient.
    cmap = plt.get_cmap('RdYlGn')
    # Select only the rgb components of the rgba array.
    image_data = cmap(image_data)
    logger.info("image_data (after cmap): {}".format(image_data))
    logger.info("image_data.shape (after cmap): {}".format(image_data.shape))
    logger.info("image_data (after cmap) min/mean/max: {}, {}, {}"
                .format(image_data.min(), image_data.mean(), image_data.max()))
    # 2.3.2. Second, color regions in which the change was outside
    #        the optional user-specified change value range.
    change_out_of_range_color = mpl.colors.to_rgba('black')
    logger.info("change_out_of_range_color: {}".format(change_out_of_range_color))
    cng_min, cng_max = task.change_threshold_min, task.change_threshold_max
    if cng_min is not None and cng_max is not None:
        diff_composite_out_of_range = (diff_data < cng_min) ^ (cng_max < diff_data)
        logger.info("diff_composite_out_of_range.sum(): {}"
                    .format(diff_composite_out_of_range.sum()))
        logger.info("diff_composite_out_of_range.shape: {}"
                    .format(diff_composite_out_of_range.shape))
        image_data[diff_composite_out_of_range] = change_out_of_range_color
        logger.info("image_data[diff_composite_out_of_range]: {}"
                    .format(image_data[diff_composite_out_of_range]))
        logger.info("image_data[diff_composite_out_of_range].shape: {}"
                    .format(image_data[diff_composite_out_of_range].shape))
    logger.info("image_data after coloring change region: {}".format(image_data))
    # 2.3.3. Third, color regions in which either the baseline or analysis
    #        composite was outside the user-specified composite value range.
    composite_out_of_range_color = mpl.colors.to_rgba('white')
    logger.info("composite_out_of_range_color: {}".format(composite_out_of_range_color))
    logger.info("orig_composite_out_of_range.shape: {}"
                .format(orig_composite_out_of_range.shape))
    image_data[orig_composite_out_of_range] = composite_out_of_range_color
    #  2.3.4. Fourth, color regions in which either the baseline or analysis
    #         composite was the no_data value as transparent.
    composite_no_data_color = np.array([0.,0.,0.,0.])
    image_data[composite_no_data] = composite_no_data_color

    logger.info("image_data before plot: {}".format(image_data))
    plt.imsave(task.result_path, image_data)

    # Plot metadata.
    dates = list(map(lambda x: datetime.strptime(x, "%m/%d/%Y"), task._get_field_as_list('acquisition_list')))
    if len(dates) > 1:
        task.plot_path = os.path.join(task.get_result_path(), "plot_path.png")
        create_2d_plot(
            task.plot_path,
            dates=dates,
            datasets=task._get_field_as_list('clean_pixel_percentages_per_acquisition'),
            data_labels="Clean Pixel Percentage (%)",
            titles="Clean Pixel Percentage Per Acquisition")

    logger.info("All products created.")
    task.complete = True
    task.execution_end = datetime.now()
    task.update_status("OK", "All products have been generated. Your result will be loaded on the map.")
    shutil.rmtree(task.get_temp_path())
    return True
