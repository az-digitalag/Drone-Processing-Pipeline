#!/usr/bin/env python

'''Extractor template for plot-level algorithms
'''

import os
import logging
import math
import random
import time
import csv
import requests
import osr
import numpy as np
import gdal

from osgeo import ogr

import pyclowder.datasets as clowder_dataset
from pyclowder.utils import CheckMessage

from terrautils.extractors import TerrarefExtractor, build_metadata, timestamp_to_terraref, \
        file_filtered_in, terraref_timestamp_to_iso
from terrautils.imagefile import file_is_image_type, image_get_geobounds, get_epsg
from terrautils.sensors import STATIONS
from terrautils.metadata import prepare_pipeline_metadata
from terrautils.betydb import get_bety_key, get_bety_api
from terrautils.geostreams import create_datapoint_with_dependencies

import extractor
import configuration

# The name of the extractor to use
EXTRACTOR_NAME = None

# Calculated sensor name is derived from specified extractor name
SENSOR_NAME = None

# Number of tries to open a CSV file before we give up
MAX_CSV_FILE_OPEN_TRIES = 10

# Maximum number of seconds a single wait for file open can take
MAX_FILE_OPEN_SLEEP_SEC = 30

# Array of trait names that should have array values associated with them
TRAIT_NAME_ARRAY_VALUE = ['site']

# Mapping of default trait names to fixecd values
TRAIT_NAME_MAP = {
    'access_level': '2',
    'species': 'Unknown',
    'citation_author': 'Unknown',
    'citation_year': 'Unknown',
    'citation_title': 'Unknown',
    'method': 'Unknown'
}

# Used to generate random numbers
RANDOM_GENERATOR = None

# Variable field names
FIELD_NAME_LIST = None

def init_extraction(name, method_name):
    """Initializes the extractor environment
    Args:
       name(str): The name of the extractor
       method_name(str): Optional name of the scientific method to reference
    Return:
        N/A
    Exceptions:
        RuntimeError is raised if a missing or invalid name is passed in
    """
    # We need to add other sensor types for OpenDroneMap generated files before anything happens
    # The Sensor() class initialization defaults the sensor dictionary and we can't override
    # without many code changes
    # pylint: disable=global-statement
    global EXTRACTOR_NAME
    global SENSOR_NAME
    global TRAIT_NAME_ARRAY_VALUE
    global FIELD_NAME_LIST

    if not name:
        raise RuntimeError("Invalid name parameter passed to init_extraction")

    EXTRACTOR_NAME = name

    new_name = name.trim().replace(' ', '_').replace('\t', '_').replace('\n', '_').\
                           replace('\r', '_')
    SENSOR_NAME = new_name.tolower()

    TRAIT_NAME_ARRAY_VALUE[0] = SENSOR_NAME

    if method_name:
        TRAIT_NAME_MAP['method_name'] = method_name

    # Get the field names into a list format
    if ',' in configuration.VARIABLE_NAMES:
        FIELD_NAME_LIST = configuration.VARIABLE_NAMES.split(',')
    else:
        FIELD_NAME_LIST = [configuration.VARIABLE_NAMES]

    # Setting up the "sensor" for this extractor. The 'ua-mac' key is used as the base
    # for when other field sites (stations) are specified: see setup_overrides()
    if 'ua-mac' in STATIONS:
        if SENSOR_NAME not in STATIONS['ua-mac']:
            STATIONS['ua-mac'][SENSOR_NAME] = {'template': '{base}/{station}/Level_2/' + \
                                                           '{sensor}/{date}/{timestamp}/{filename}',
                                               'pattern': '{sensor}_L3_{station}_{date}{opts}.csv',
                                              }

def __do_initialization():
    """ Function to kick off initialization of this module by another script.
    """
    import configuration

    try:
        method_name = configuration.METHOD_NAME     # pylint: disable=no-member
    except: # pylint: disable=bare-except
        method_name = None

    init_extraction(configuration.EXTRACTOR_NAME, method_name)

# Call our "do the initialization" function
__do_initialization()

def _get_plot_name(name):
    """Looks in the parameter and returns a plot name.

       Expects the plot name to be identified by having "By Plot" embedded in the name.
       The plot name is then surrounded by " - " characters. That valus is then returned.

    Args:
        name(iterable or string): An array/list of names or a single name string

    Return:
        Returns the found plot name or an empty string.
    """
    if isinstance(name, str):
        name = [name]

    plot_signature = "by plot"
    plot_separator = " - "
    # Loop through looking for a plot identifier (case insensitive)
    for one_name in name:
        low_name = one_name.lower()
        if plot_signature in low_name:
            parts = low_name.split(plot_separator)
            parts_len = len(parts)
            if parts_len > 1:
                start_pos = len(parts[0]) + len(plot_separator)
                end_pos = start_pos + len(parts[1])
                return one_name[start_pos:end_pos]

    return ""

def _get_open_backoff(prev=None):
    """Returns the number of seconds to backoff from opening a file

    Args:
        prev(int or float): the previous return value from this function

    Return:
        Returns the number of seconds (including fractional seconds) to wait

    Note that the return value is deterministic, and always the same, when None is
    passed in
    """
    # pylint: disable=global-statement
    global RANDOM_GENERATOR
    global MAX_FILE_OPEN_SLEEP_SEC

    # Simple case
    if prev is None:
        return 1

    # Get a random number generator
    if RANDOM_GENERATOR is None:
        try:
            RANDOM_GENERATOR = random.SystemRandom()
        finally:
            # Set this so we don't try again
            RANDOM_GENERATOR = 0

    # Get a random number
    if RANDOM_GENERATOR:
        multiplier = RANDOM_GENERATOR.random()  # pylint: disable=no-member
    else:
        multiplier = random.random()

    # Calculate how long to sleep
    sleep = math.trunc(float(prev) * multiplier * 100) / 10.0
    if sleep > MAX_FILE_OPEN_SLEEP_SEC:
        sleep = max(0.1, math.trunc(multiplier * 100) / 10)

    return sleep

def update_geostreams(connector, host, secret_key, geo_csv_header, geo_rows):
    """Sends the rows of csv data to geostreams
    Args:
        connector(obj): the message queue connector instance
        host(str): the URI of the host making the connection
        secret_key(str): used with the host API
        geo_csv_header(str): comma separated list of column headers
        geo_rows(list): list of strings that are comma separated column data (list of data rows)
    Notes:
        Header names expected are: 'lat', 'lon', 'dp_time', 'timestamp', 'source', 'value', and 'trait'
    """
    data = [geo_csv_header]
    data.extend(geo_rows)

    reader = csv.DictReader(data)
    idx = 1
    for row in reader:
        centroid_lonlat = [row['lon'], row['lat']]
        time_fmt = row['dp_time']
        timestamp = row['timestamp']
        dpmetadata = {
            "source": row['source'],
            "value": row['value']
        }
        trait = row['trait']

        idx += 1
        create_datapoint_with_dependencies(connector, host, secret_key, trait,
                                           (centroid_lonlat[1], centroid_lonlat[0]), time_fmt, time_fmt,
                                           dpmetadata, timestamp)

def update_betydb(bety_csv_header, bety_rows):
    """Sends the rows of csv data to BETYdb
    Args:
        bety_csv_header(str): comma separated list of column headers
        bety_rows(list): list of strings that are comma separated column data (list of data rows)
    """
    betyurl = get_bety_api('traits')
    request_params = {'key': get_bety_key()}
    filetype = 'csv'
    content_type = 'text/csv'
    data = [bety_csv_header]
    data.extend(bety_rows)

    resp = requests.post("%s.%s" % (betyurl, filetype), params=request_params,
                         data=os.linesep.join(data),
                         headers={'Content-type': content_type})

    if resp.status_code in [200, 201]:
        logging.info("Data successfully submitted to BETYdb.")
        return resp.json()['data']['ids_of_new_traits']
    else:
        logging.error("Error submitting data to BETYdb: %s -- %s", resp.status_code, resp.reason)
        resp.raise_for_status()

    return None

def get_bety_fields():
    """Returns the supported field names as a list
    """
    # pylint: disable=global-statement
    global FIELD_NAME_LIST

    return ('local_datetime', 'access_level', 'species', 'site', 'citation_author', 'citation_year',
            'citation_title', 'method') + FIELD_NAME_LIST

def get_geo_fields():
    """Returns the supported field names as a list
    """
    return ('site', 'trait', 'lat', 'lon', 'dp_time', 'source', 'value', 'timestamp')


def get_default_trait(trait_name):
    """Returns the default value for the trait name
    Args:
       trait_name(str): the name of the trait to return the default value for
    Return:
        If the default value for a trait is configured, that value is returned. Otherwise
        an empty string is returned.
    """
    # pylint: disable=global-statement
    global TRAIT_NAME_ARRAY_VALUE
    global TRAIT_NAME_MAP

    if trait_name in TRAIT_NAME_ARRAY_VALUE:
        return []   # Return an empty list when the name matches
    elif trait_name in TRAIT_NAME_MAP:
        return TRAIT_NAME_MAP[trait_name]
    return ""

def get_bety_traits_table():
    """Returns the field names and default trait values

    Returns:
        A tuple containing the list of field names and a dictionary of default field values
    """
    # Compiled traits table
    fields = get_bety_fields()
    traits = {}
    for field_name in fields:
        traits[field_name] = get_default_trait(field_name)

    return (fields, traits)

def get_geo_traits_table():
    """Returns the field names and default trait values

    Returns:
        A tuple containing the list of field names and a dictionary of default field values
    """
    fields = get_geo_fields()
    traits = {}
    for field_name in fields:
        traits[field_name] = ""

    return (fields, traits)

def generate_traits_list(fields, traits):
    """Returns an array of trait values

    Args:
        fields(list): the list of fields to look up and return
        traits(dict): contains the set of trait values to return

    Return:
        Returns an array of trait values taken from the traits parameter

    Notes:
        If a trait isn't found, it's assigned an empty string
    """
    # compose the summary traits
    trait_list = []
    for field_name in fields:
        if field_name in traits:
            trait_list.append(traits[field_name])
        else:
            trait_list.append(get_default_trait(field_name))

    return trait_list

# The class for plot level extractors
class PlotExtractor(TerrarefExtractor):
    """Extractor for calculating values from an RGB image representing a plot

       The extractor creates a CSV file with the data which is then uploaded to BETYdb.
       Will also update the dataset metadata in Clowder with the calculated value(s) and
       writes the values to a CSV file
    """
    def __init__(self):
        """Initialization of class instance.

           We use the identify application to identify the mime type of files and then
           determine if they are georeferenced using the osgeo package
        """
        super(PlotExtractor, self).__init__()

        # Our default values
        identify_binary = os.getenv('IDENTIFY_BINARY', '/usr/bin/identify')

        # Add any additional arguments to parser
        self.parser.add_argument('--identify-binary', nargs='?', dest='identify_binary',
                                 default=identify_binary,
                                 help='Identify executable used to for image type capture ' +
                                 '(default=' + identify_binary + ')')

        # Confirm that setup has happened
        if EXTRACTOR_NAME is None:
            raise RuntimeError("Please call init_extraction(name) before creating an instance " + \
                               "of the extractor class")

        # parse command line and load default logging configuration
        self.setup(sensor=SENSOR_NAME)

    # List of file extensions we will probably see that we don't need to check for being
    # an image type
    @property
    def known_non_image_ext(self):
        """Returns an array of file extensions that we will see that
           are definitely not an image type
        """
        return ["dbf", "json", "prj", "shp", "shx", "txt"]

    # Look through the file list to find the files we need
    # pylint: disable=too-many-locals,too-many-nested-blocks
    def find_image_files(self, files):
        """Finds files that are needed for extracting plots from an orthomosaic

        Args:
            files(list): the list of file to look through and access

        Returns:
            Returns a dict of georeferenced image files (indexed by filename and containing an
            object with the calculated image bounds as an ogr polygon and a list of the
            bounds as a tuple)

            The bounds are assumed to be rectilinear with the upper-left corner directly
            pulled from the file and the lower-right corner calculated based upon the geometry
            information stored in the file.

            The polygon points start at the upper left corner and proceed clockwise around the
            boundary. The returned polygon is closed: the first and last point are the same.

            The bounds tuple contains the min and max Y point values, followed by the min and
            max X point values.
        """
        imagefiles = {}

        for onefile in files:
            ext = os.path.splitext(os.path.basename(onefile))[1].lstrip('.')
            if not ext in self.known_non_image_ext:
                if file_is_image_type(self.args.identify_binary, onefile,
                                      onefile + self.file_infodata_file_ending):
                    # If the file has a geo shape we store it for clipping
                    bounds = image_get_geobounds(onefile)
                    epsg = get_epsg(onefile)
                    if bounds[0] != np.nan:
                        ring = ogr.Geometry(ogr.wkbLinearRing)
                        ring.AddPoint(bounds[2], bounds[1])     # Upper left
                        ring.AddPoint(bounds[3], bounds[1])     # Upper right
                        ring.AddPoint(bounds[3], bounds[0])     # lower right
                        ring.AddPoint(bounds[2], bounds[0])     # lower left
                        ring.AddPoint(bounds[2], bounds[1])     # Closing the polygon

                        poly = ogr.Geometry(ogr.wkbPolygon)
                        poly.AddGeometry(ring)

                        ref_sys = osr.SpatialReference()
                        if ref_sys.ImportFromEPSG(int(epsg)) != ogr.OGRERR_NONE:
                            logging.error("Failed to import EPSG %s for image file %s",
                                          str(epsg), onefile)
                        else:
                            poly.AssignSpatialReference(ref_sys)

                        imagefiles[onefile] = {'bounds' : poly}

        # Return what we've found
        return imagefiles

    # Make a best effort to get a dataset ID
    # pylint: disable=no-self-use
    def get_dataset_id(self, host, key, resource, dataset_name=None):
        """Makes a best effort attempt to get a dataset ID

        Args:
            host(str): the URI of the host making the connection
            secret_key(str): used with the host API
            resource(dict): dictionary containing the resources associated with the request
            dataset_name(str): optional parameter containing the dataset name of interest

        Return:
            The found dataset ID or None

        Note:
            The resource parameter is investigated first for a dataset ID. Note that if found,
            this dataset ID may not represent the dataset_name (if specified).

            If the resource parameter is not specified, or doesn't have the expected elements
            then a dataset lookup is performed
        """
        # First check to see if the ID is provided
        if resource and 'type' in resource:
            if resource['type'] == 'dataset':
                return resource['id']
            elif resource['type'] == 'file':
                if ('parent' in resource) and ('id' in resource['parent']):
                    return resource['parent']['id']

        # Look through all the datasets we can retrieve to find the ID
        if dataset_name:
            url = '%s/api/datasets/sorted' % (host)
            params = {"key" : key, "limit" : 50000}
            headers = {'content-type': 'application/json'}

            response = requests.get(url, headers=headers, params=params, verify=False)
            response.raise_for_status()
            datasets = response.json()

            for one_ds in datasets:
                if 'name' in one_ds and 'id' in one_ds:
                    if one_ds['name'] == dataset_name:
                        return one_ds['id']

        return None

    def write_csv_file(self, resource, filename, header, data):
        """Attempts to write out the data to the specified file. Will write the
           header information if it's the first call to write to the file.

           If the file is not available, it waits as configured until it becomes
           available, or returns an error.

           Args:
                resource(dict): dictionary containing the resources associated with the request
                filename(str): path to the file to write to
                header(str): Optional CSV formatted header to write to the file; can be set to None
                data(str): CSV formatted data to write to the file

            Return:
                Returns True if the file was written to and False otherwise
        """
        # pylint: disable=global-statement
        global MAX_CSV_FILE_OPEN_TRIES

        if not resource or not filename or not data:
            self.log_error(resource, "Empty parameter passed to write_geo_csv")
            return False

        csv_file = None
        backoff_secs = None
        for tries in range(0, MAX_CSV_FILE_OPEN_TRIES):
            try:
                csv_file = open(filename, 'a+')
            except Exception as ex:     # pylint: disable=broad-except
                pass

            if csv_file:
                break

            # If we can't open the file, back off and try again (unless it's our last try)
            if tries < MAX_CSV_FILE_OPEN_TRIES - 1:
                backoff_secs = _get_open_backoff(backoff_secs)
                self.log_info(resource, "Sleeping for " + str(backoff_secs) + \
                                                " seconds before trying to open CSV file again")
                time.sleep(backoff_secs)

        if not csv_file:
            self.log_error(resource, "Unable to open CSV file for writing: '" + filename + "'")
            self.log_error(resource, "Exception: " + str(ex))
            return False

        wrote_file = False
        try:
            # Check if we need to write a header
            if os.fstat(csv_file.fileno()).st_size <= 0:
                csv_file.write(header + "\n")

            # Write out data
            csv_file.write(data + "\n")

            wrote_file = True
        except Exception as ex:
            self.log_error(resource, "Exception while writing CSV file: '" + filename + "'")
            self.log_error(resource, "Exception: " + str(ex))
        finally:
            csv_file.close()

        # Return whether or not we wrote to the file
        return wrote_file

    # Entry point for checking how message should be handled
    def check_message(self, connector, host, secret_key, resource, parameters):
        """Determines if we want to handle the received message

        Args:
            connector(obj): the message queue connector instance
            host(str): the URI of the host making the connection
            secret_key(str): used with the host API
            resource(dict): dictionary containing the resources associated with the request
            parameters(json): json object of the triggering message contents
        """
        self.start_check(resource)

        if (resource['triggering_file'] is None) or (resource['triggering_file'].endswith(".tif")):
            dataset_id = None
            if resource['type'] == 'dataset':
                dataset_id = resource['id']
            elif resource['type'] == 'file':
                if ('parent' in resource) and ('id' in resource['parent']):
                    dataset_id = resource['parent']['id']
            if dataset_id:
                return CheckMessage.download

        return CheckMessage.ignore

    # Entry point for processing messages
    # pylint: disable=too-many-arguments, too-many-branches, too-many-statements, too-many-locals, attribute-defined-outside-init
    def process_message(self, connector, host, secret_key, resource, parameters):
        """Performs plot level image extraction

        Args:
            connector(obj): the message queue connector instance
            host(str): the URI of the host making the connection
            secret_key(str): used with the host API
            resource(dict): dictionary containing the resources associated with the request
            parameters(json): json object of the triggering message contents
        """
        # pylint: disable=global-statement
        global SENSOR_NAME
        global FIELD_NAME_LIST

        self.start_message(resource)
        super(PlotExtractor, self).process_message(connector, host, secret_key, resource, parameters)

        # Initialize local variables
        dataset_name = resource["name"]
        experiment_name = "Unknown Experiment"
        datestamp = None
        citation_auth_override, citation_title_override, citation_year_override = None, None, None
        config_specie = None

        store_in_geostreams = True
        store_in_betydb = True
        create_csv_files = True
        out_geo = None
        out_csv = None

        # Find the files we're interested in
        imagefiles = self.find_image_files(resource['local_paths'])
        num_image_files = len(imagefiles)
        if num_image_files <= 0:
            self.log_skip(resource, "No image files with geographic boundaries found")
            return

        # Setup overrides and get the restore function
        restore_fn = self.setup_overrides(host, secret_key, resource)
        if not restore_fn:
            self.end_message(resource)
            return

        try:
            # Get the best timestamp
            timestamp = terraref_timestamp_to_iso(self.find_timestamp(resource['dataset_info']['name']))
            if 'T' in timestamp:
                datestamp = timestamp.split('T')[0]
            else:
                datestamp = timestamp
                timestamp += 'T12:00:00'
            if timestamp.find('T') > 0 and timestamp.rfind('-') > 0 and timestamp.find('T') < timestamp.rfind('-'):
                # Convert to local time. We can do this due to site definitions having
                # the time offsets as part of their definition
                localtime = timestamp[0:timestamp.rfind('-')]
            else:
                localtime = timestamp
            _, experiment_name, _ = self.get_season_and_experiment(timestamp_to_terraref(timestamp),
                                                                   self.sensor_name)

            # Build up a list of image IDs
            image_ids = {}
            if 'files' in resource:
                for one_image in imagefiles:
                    image_name = os.path.basename(one_image)
                    for res_file in resource['files']:
                        if ('filename' in res_file) and ('id' in res_file) and \
                                                            (image_name == res_file['filename']):
                            image_ids[image_name] = res_file['id']

            file_filters = self.get_file_filters()
            if self.experiment_metadata:
                extractor_json = self.find_extractor_json()
                if extractor_json:
                    if 'citationAuthor' in extractor_json:
                        citation_auth_override = extractor_json['citationAuthor']
                    if 'citationYear' in extractor_json:
                        citation_year_override = extractor_json['citationYear']
                    if 'citationTitle' in extractor_json:
                        citation_title_override = extractor_json['citationTitle']
                    if 'noGeostreams' in extractor_json:
                        store_in_geostreams = False
                    if 'noBETYdb' in extractor_json:
                        store_in_betydb = False
                    if 'noCSV' in extractor_json:
                        create_csv_files = False

                if 'germplasmName' in self.experiment_metadata:
                    config_specie = self.experiment_metadata['germplasmName']

            # Create the output files
            rootdir = self.sensors.create_sensor_path(timestamp, sensor=SENSOR_NAME, ext=".csv",
                                                      opts=[experiment_name])
            (bety_fields, bety_traits) = get_bety_traits_table()
            (geo_fields, geo_traits) = get_geo_traits_table()

            if create_csv_files:
                out_geo = os.path.splitext(rootdir)[0] + "_" + SENSOR_NAME + "_geo.csv"
                self.log_info(resource, "Writing Geostreams CSV to %s" % out_geo)
                out_csv = os.path.splitext(rootdir)[0] + "_" + SENSOR_NAME + ".csv"
                self.log_info(resource, "Writing Shapefile CSV to %s" % out_csv)

            # Setup default trait values
            if not config_specie is None:
                bety_traits['species'] = config_specie
            if not citation_auth_override is None:
                bety_traits['citation_author'] = citation_auth_override
            if not citation_title_override is None:
                bety_traits['citation_title'] = citation_title_override
            if not citation_year_override is None:
                bety_traits['citation_year'] = citation_year_override
            else:
                bety_traits['citation_year'] = datestamp[:4]

            bety_csv_header = ','.join(map(str, bety_fields))
            geo_csv_header = ','.join(map(str, geo_fields))

            # Loop through all the images (of which there should be one - see above)
            geo_rows = []
            bety_rows = []
            len_field_value = len(FIELD_NAME_LIST)
            for filename in imagefiles:

                # Check if we're filtering files
                if file_filters:
                    if not file_filtered_in(filename, file_filters):
                        continue

                try:
                    calc_value = ""

                    # Load the pixels
                    clip_pix = np.array(gdal.Open(filename).ReadAsArray())

                    # Get additional, necessary data
                    centroid = imagefiles[filename]["bounds"].Centroid()
                    plot_name = _get_plot_name([resource['dataset_info']['name'], dataset_name])

                    calc_value = extractor.calculate(np.rollaxis(clip_pix, 0, 3))

                    # Convert to something iterable that's in the correct order
                    if isinstance(calc_value, set):
                        raise RuntimeError("A 'set' type of data was returned and isn't supported. " \
                                           "Please use a list or a tuple instead")
                    elif isinstance(calc_value, dict):
                        # Assume the dictionary is going to have field names with their values
                        # We check whether we have the correct number of fields later. This also
                        # filters out extra fields
                        values = []
                        for key in FIELD_NAME_LIST:
                            if key in calc_value:
                                values.append(calc_value[key])
                    elif not isinstance(calc_value, (list, tuple)):
                        values = [calc_value]

                    # Sanity check our values
                    len_calc_value = len(calc_value)
                    if not len_calc_value == len_field_value:
                        raise RuntimeError("Incorrect number of values returned. Expected " + str(len_field_value) +
                                           " and received " + str(len_calc_value))

                    # Prepare the data for writing
                    image_clowder_id = ""
                    image_name = os.path.basename(filename)
                    if image_name in image_ids:
                        image_clowder_id = image_ids[image_name]
                    geo_traits['site'] = plot_name
                    geo_traits['lat'] = str(centroid.GetY())
                    geo_traits['lon'] = str(centroid.GetX())
                    geo_traits['dp_time'] = localtime
                    geo_traits['source'] = host.rstrip('/') + '/files/' + str(image_clowder_id)
                    geo_traits['timestamp'] = datestamp

                    # Write the data points geographically and otherwise
                    for idx in range(0, len_field_value):
                        # The way the code is configured, Geostreams can only handle one field
                        # at a time so we write out one row per field/value pair
                        geo_traits['trait'] = FIELD_NAME_LIST[idx]
                        geo_traits['value'] = str(values[idx])
                        trait_list = generate_traits_list(geo_fields, geo_traits)
                        csv_data = ','.join(map(str, trait_list))
                        if out_geo:
                            self.write_csv_file(resource, out_geo, geo_csv_header, csv_data)
                        if store_in_geostreams:
                            geo_rows.append(csv_data)

                        # BETYdb can handle wide rows with multiple values so we just set the field
                        # values here and write the single row after the loop
                        bety_traits[FIELD_NAME_LIST[idx]] = str(values[idx])

                    bety_traits['site'] = plot_name
                    bety_traits['local_datetime'] = localtime
                    trait_list = generate_traits_list(bety_fields, bety_traits)
                    csv_data = ','.join(map(str, trait_list))
                    if out_csv:
                        self.write_csv_file(resource, out_csv, bety_csv_header, csv_data)
                    if store_in_betydb:
                        bety_rows.append(csv_data)

                except Exception as ex:
                    self.log_error(resource, "error generating " + EXTRACTOR_NAME + " for %s" % plot_name)
                    self.log_error(resource, "    exception: %s" % str(ex))
                    continue

                # Only process the first file that's valid
                if num_image_files > 1:
                    self.log_info(resource, "Multiple image files were found, only using first found")
                    break

            # Upload any geostreams or betydb data
            if store_in_geostreams:
                if geo_rows:
                    update_geostreams(connector, host, secret_key, geo_csv_header, geo_rows)
                else:
                    self.log_info(resource, "No geostreams data was generated to upload")

            if store_in_betydb:
                if bety_rows:
                    update_betydb(bety_csv_header, bety_rows)
                else:
                    self.log_info(resource, "No BETYdb data was generated to upload")

            # Update this dataset with the extractor info
            dataset_id = self.get_dataset_id(host, secret_key, resource, dataset_name)
            try:
                # Tell Clowder this is completed so subsequent file updates don't daisy-chain
                self.log_info(resource, "updating dataset metadata")
                content = {"comment": "Calculated " + SENSOR_NAME + " index",
                           SENSOR_NAME + " value": calc_value
                          }
                if self.experiment_metadata:
                    content.update(prepare_pipeline_metadata(self.experiment_metadata))
                extractor_md = build_metadata(host, self.extractor_info, dataset_id, content,
                                              'dataset')
                clowder_dataset.remove_metadata(connector, host, secret_key, dataset_id,
                                                self.extractor_info['name'])
                clowder_dataset.upload_metadata(connector, host, secret_key, dataset_id,
                                                extractor_md)

            except Exception as ex:
                self.log_error(resource, "Exception updating dataset metadata: " + str(ex))
        finally:
            # Signal end of processing message and restore changed variables. Be sure to restore
            # changed variables above with early returns
            if restore_fn:
                restore_fn()
            self.end_message(resource)

if __name__ == "__main__":
    extractor = PlotExtractor()     # pylint: disable=invalid-name
    extractor.start()
