"""
Routines for transforming FERC Form 1 data before loading into the PUDL DB.

This module provides a variety of functions that are used in cleaning up the
FERC Form 1 data prior to loading into our database. This includes adopting
standardized units and column names, standardizing the formatting of some
string values, and correcting data entry errors which we can infer based on
the existing data. It may also include removing bad data, or replacing it
with the appropriate NA values.
"""

import os.path
from difflib import SequenceMatcher
import pandas as pd
import numpy as np

# These modules are required for the FERC Form 1 Plant ID & Time Series
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import Normalizer, RobustScaler
from sklearn.preprocessing import OneHotEncoder

import pudl.constants as pc
import pudl.transform.pudl
import pudl.helpers
from pudl.settings import SETTINGS

##############################################################################
# FERC TRANSFORM HELPER FUNCTIONS ############################################
##############################################################################


def _clean_cols(df):
    """
    Add a FERC record ID and drop FERC columns not to be loaded into PUDL.

    It is often useful to be able to tell exactly which record in the FERC Form
    1 database a given record within the PUDL database came from. Within each
    FERC Form 1 table, each record is uniquely identified by the combination
    of:
      - report_year
      - respondent_id
      - spplmnt_num
      - row_number

    So this function takes a dataframe, checks to make sure it contains each of
    those columns and that none of them are NULL, and adds a new column to the
    dataframe containing a string of the format:

    {report_year}_{respondent_id}_{spplmnt_num}_{row_number}

    In addition there are some columns which are not meaningful or useful in
    the context of PUDL, but which show up in virtually every FERC table, and
    this function drops them if they are present. These columns include:
     - spplmnt_num (which goes into the record ID)
     - row_number (which goes into the record_ ID)
     - row_prvlg
     - row_seq
     - item
     - report_prd
     - record_number (temp column used in plants_small)
    """
    assert ~df.report_year.isnull().any()
    assert ~df.respondent_id.isnull().any()
    assert ~df.spplmnt_num.isnull().any()
    assert ~df.row_number.isnull().any()
    # Create a unique inter-year FERC table record ID:
    df['record_id'] = \
        df.report_year.astype(str) + '_' + \
        df.respondent_id.astype(str) + '_' + \
        df.spplmnt_num.astype(str) + '_' + \
        df.row_number.astype(str)

    unused_cols = [
        'spplmnt_num',
        'row_number',
        'row_prvlg',
        'row_seq',
        'report_prd',
        'item',
        'record_number'
    ]
    df = df.drop([col for col in unused_cols if col in df.columns], axis=1)

    return df


def _multiplicative_error_correction(tofix, mask, minval, maxval, mults):
    """
    Correct data entry errors resulting in data being multiplied by a factor.

    In many cases we know that a particular column in the database should have
    a value in a particular rage (e.g. the heat content of a ton of coal is a
    well defined physical quantity -- it can be 15 mmBTU/ton or 22 mmBTU/ton,
    but it can't be 1 mmBTU/ton or 100 mmBTU/ton). Sometimes these fields
    are reported in the wrong units (e.g. kWh of electricity generated rather
    than MWh) resulting in several distributions that have a similar shape
    showing up at different ranges of value within the data.  This function
    takes a one dimensional data series, a description of a valid range for
    the values, and a list of factors by which we expect to see some of the
    data multiplied due to unit errors.  Data found in these "ghost"
    distributions are multiplied by the appropriate factor to bring them into
    the expected range.

    Data values which are not found in one of the acceptable multiplicative
    ranges are set to NA.

    Args:
        tofix (pandas.Series): A 1-dimensional data series containing the
            values to be fixed.
        mask (pandas.Series): A 1-dimensional masking array of True/False
            values, which will be used to select a subset of the tofix
            series onto which we will apply the multiplicative fixes.
        min (float): the minimum realistic value for the data series.
        max (float): the maximum realistic value for the data series.
        mults (list of floats): values by which "real" data may have been
            multiplied due to common data entry errors. These values both
            show us where to look in the full data series to find recoverable
            data, and also tell us by what factor those values need to be
            multiplied to bring them back into the reasonable range.
    Returns:
        fixed (pandas.Series): a data series of the same length as the
            input, but with the transformed values.
    """
    # Grab the subset of the input series we are going to work on:
    records_to_fix = tofix[mask]
    # Drop those records from our output series
    fixed = tofix.drop(records_to_fix.index)
    # Iterate over the multipliers, applying fixes to outlying populations
    for mult in mults:
        records_to_fix = records_to_fix.apply(lambda x: x * mult
                                              if x > minval / mult
                                              and x < maxval / mult
                                              else x)
    # Set any record that wasn't inside one of our identified populations to
    # NA -- we are saying that these are true outliers, which can't be part
    # of the population of values we are examining.
    records_to_fix = records_to_fix.apply(lambda x: np.nan
                                          if x < minval
                                          or x > maxval
                                          else x)
    # Add our fixed records back to the complete data series and return it
    fixed = fixed.append(records_to_fix)
    return fixed


##############################################################################
# DATABASE TABLE SPECIFIC PROCEDURES ##########################################
##############################################################################
def fuel(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 fuel data for loading into PUDL Database.

    This process includes converting some columns to be in terms of our
    preferred units, like MWh and mmbtu instead of kWh and btu. Plant names are
    also standardized (stripped & Title Case). Fuel and fuel unit strings are
    also standardized using our cleanstrings() function and string cleaning
    dictionaries found in pudl.constants.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs, clean it up a bit
    fuel_ferc1_df = _clean_cols(ferc1_raw_dfs['fuel_ferc1'])

    #########################################################################
    # STANDARDIZE NAMES AND CODES ###########################################
    #########################################################################
    # Standardize plant_name capitalization and remove leading/trailing white
    # space -- necesary b/c plant_name is part of many foreign keys.
    fuel_ferc1_df = pudl.helpers.strip_lower(fuel_ferc1_df, ['plant_name'])

    # Take the messy free-form fuel & fuel_unit fields, and do our best to
    # map them to some canonical categories... this is necessarily imperfect:
    fuel_ferc1_df.fuel = \
        pudl.transform.pudl.cleanstrings(fuel_ferc1_df.fuel,
                                         pc.ferc1_fuel_strings,
                                         unmapped='')

    fuel_ferc1_df.fuel_unit = \
        pudl.transform.pudl.cleanstrings(fuel_ferc1_df.fuel_unit,
                                         pc.ferc1_fuel_unit_strings,
                                         unmapped='')

    #########################################################################
    # PERFORM UNIT CONVERSIONS ##############################################
    #########################################################################

    # Replace fuel cost per kWh with fuel cost per MWh
    fuel_ferc1_df['fuel_cost_per_mwh'] = 1000 * fuel_ferc1_df['fuel_cost_kwh']
    fuel_ferc1_df.drop('fuel_cost_kwh', axis=1, inplace=True)

    # Replace BTU/kWh with millions of BTU/MWh
    fuel_ferc1_df['fuel_mmbtu_per_mwh'] = (1e3 / 1e6) * \
        fuel_ferc1_df['fuel_generaton']
    fuel_ferc1_df.drop('fuel_generaton', axis=1, inplace=True)

    # Convert from BTU/unit of fuel to 1e6 BTU/unit.
    fuel_ferc1_df['fuel_avg_mmbtu_per_unit'] = fuel_ferc1_df['fuel_avg_heat'] / 1e6
    fuel_ferc1_df.drop('fuel_avg_heat', axis=1, inplace=True)

    #########################################################################
    # RENAME COLUMNS TO MATCH PUDL DB #######################################
    #########################################################################
    fuel_ferc1_df.rename(columns={
        # FERC 1 DB Name      PUDL DB Name
        'respondent_id': 'utility_id_ferc',
        'fuel': 'fuel_type_code_pudl',
        'fuel_avg_mmbtu_per_unit': 'fuel_mmbtu_per_unit',
        'fuel_quantity': 'fuel_qty_burned',
        'fuel_cost_burned': 'fuel_cost_per_unit_burned',
        'fuel_cost_delvd': 'fuel_cost_per_unit_delivered',
        'fuel_cost_btu': 'fuel_cost_per_mmbtu'},
        inplace=True)

    #########################################################################
    # CORRECT DATA ENTRY ERRORS #############################################
    #########################################################################
    coal_mask = fuel_ferc1_df['fuel_type_code_pudl'] == 'coal'
    gas_mask = fuel_ferc1_df['fuel_type_code_pudl'] == 'gas'
    oil_mask = fuel_ferc1_df['fuel_type_code_pudl'] == 'oil'

    corrections = [
        # mult = 2000: reported in units of lbs instead of short tons
        # mult = 1e6:  reported BTUs instead of mmBTUs
        # minval and maxval of 10 and 29 mmBTUs are the range of values
        # specified by EIA 923 instructions at:
        # https://www.eia.gov/survey/form/eia_923/instructions.pdf
        ['fuel_mmbtu_per_unit', coal_mask, 10.0, 29.0, (2e3, 1e6)],

        # mult = 1e-2: reported cents/mmBTU instead of USD/mmBTU
        # minval and maxval of .5 and 7.5 dollars per mmBTUs are the
        # end points of the primary distribution of EIA 923 fuel receipts
        # and cost per mmBTU data weighted by quantity delivered
        ['fuel_cost_per_mmbtu', coal_mask, 0.5, 7.5, (1e-2, )],

        # mult = 1e3: reported fuel quantity in cubic feet, not mcf
        # mult = 1e6: reported fuel quantity in BTU, not mmBTU
        # minval and maxval of .8 and 1.2 mmBTUs are the range of values
        # specified by EIA 923 instructions
        ['fuel_mmbtu_per_unit', gas_mask, 0.8, 1.2, (1e3, 1e6)],

        # mult = 1e-2: reported in cents/mmBTU instead of USD/mmBTU
        # minval and maxval of 1 and 35 dollars per mmBTUs are the
        # end points of the primary distribution of EIA 923 fuel receipts
        # and cost per mmBTU data weighted by quantity delivered
        ['fuel_cost_per_mmbtu', gas_mask, 1, 35, (1e-2, )],

        # mult = 42: reported fuel quantity in gallons, not barrels
        # mult = 1e6: reported fuel quantity in BTU, not mmBTU
        # minval and maxval of 3 and 6.9 mmBTUs are the range of values
        # specified by EIA 923 instructions
        ['fuel_mmbtu_per_unit', oil_mask, 3, 6.9, (42, )],

        # mult = 1e-2: reported in cents/mmBTU instead of USD/mmBTU
        # minval and maxval of 5 and 33 dollars per mmBTUs are the
        # end points of the primary distribution of EIA 923 fuel receipts
        # and cost per mmBTU data weighted by quantity delivered
        ['fuel_cost_per_mmbtu', oil_mask, 5, 33, (1e-2, )]
    ]

    for (coltofix, mask, minval, maxval, mults) in corrections:
        fuel_ferc1_df[coltofix] = \
            _multiplicative_error_correction(fuel_ferc1_df[coltofix],
                                             mask, minval, maxval, mults)

    #########################################################################
    # REMOVE BAD DATA #######################################################
    #########################################################################
    # Drop any records that are missing data. This is a blunt instrument, to
    # be sure. In some cases we lose data here, because some utilities have
    # (for example) a "Total" line w/ only fuel_mmbtu_per_kwh on it. Grr.
    fuel_ferc1_df.dropna(inplace=True)

    ferc1_transformed_dfs['fuel_ferc1'] = fuel_ferc1_df

    return ferc1_transformed_dfs


def plants_steam(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 plant_steam data for loading into PUDL Database.

    This includes converting to our preferred units of MWh and MW, as well as
    standardizing the strings describing the kind of plant and construction.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_steam_df = _clean_cols(ferc1_raw_dfs['plants_steam_ferc1'])

    # Standardize plant_name capitalization and remove leading/trailing white
    # space -- necesary b/c plant_name is part of many foreign keys.
    ferc1_steam_df = pudl.helpers.strip_lower(ferc1_steam_df, ['plant_name'])

    # Take the messy free-form construction_type and plant_kind fields, and do
    # our best to map them to some canonical categories...
    # this is necessarily imperfect:

    ferc1_steam_df.type_const = \
        pudl.transform.pudl.cleanstrings(ferc1_steam_df.type_const,
                                         pc.ferc1_construction_type_strings,
                                         unmapped='')

    ferc1_steam_df.plant_kind = \
        pudl.transform.pudl.cleanstrings(ferc1_steam_df.plant_kind,
                                         pc.ferc1_plant_kind_strings,
                                         unmapped='')

    # Force the construction and installation years to be numeric values, and
    # set them to NA if they can't be converted. (table has some junk values)
    ferc1_steam_df['yr_const'] = pd.to_numeric(
        ferc1_steam_df['yr_const'],
        errors='coerce')
    ferc1_steam_df['yr_installed'] = pd.to_numeric(
        ferc1_steam_df['yr_installed'],
        errors='coerce')

    # Converting everything to per MW and MWh units...
    ferc1_steam_df['cost_per_mw'] = 1000 * ferc1_steam_df['cost_per_kw']
    ferc1_steam_df.drop('cost_per_kw', axis=1, inplace=True)
    ferc1_steam_df['net_generation_mwh'] = \
        ferc1_steam_df['net_generation'] / 1000
    ferc1_steam_df.drop('net_generation', axis=1, inplace=True)
    ferc1_steam_df['expns_per_mwh'] = 1000 * ferc1_steam_df['expns_kwh']
    ferc1_steam_df.drop('expns_kwh', axis=1, inplace=True)

    ferc1_steam_df.rename(columns={
        # FERC 1 DB Name      PUDL DB Name
        'respondent_id': 'utility_id_ferc',
        'yr_const': 'construction_year',
        'plant_kind': 'plant_type',
        'type_const': 'construction_type',
        'asset_retire_cost': 'asset_retirement_cost',
        'yr_installed': 'installation_year',
        'tot_capacity': 'capacity_mw',
        'peak_demand': 'peak_demand_mw',
        'plant_hours': 'plant_hours_connected_while_generating',
        'plnt_capability': 'plant_capability_mw',
        'when_limited': 'water_limited_capacity_mw',
        'when_not_limited': 'not_water_limited_capacity_mw',
        'avg_num_of_emp': 'avg_num_employees',
        'net_generation': 'net_generation_mwh',
        'cost_land': 'capex_land',
        'cost_structure': 'capex_structure',
        'cost_equipment': 'capex_equipment',
        'cost_of_plant_to': 'capex_total',
        'cost_per_mw': 'capex_per_mw',
        'expns_operations': 'opex_operations',
        'expns_fuel': 'opex_fuel',
        'expns_coolants': 'opex_coolants',
        'expns_steam': 'opex_steam',
        'expns_steam_othr': 'opex_steam_other',
        'expns_transfer': 'opex_transfer',
        'expns_electric': 'opex_electric',
        'expns_misc_power': 'opex_misc_power',
        'expns_rents': 'opex_rents',
        'expns_allowances': 'opex_allowances',
        'expns_engnr': 'opex_engineering',
        'expns_structures': 'opex_structures',
        'expns_boiler': 'opex_boiler',
        'expns_plants': 'opex_plants',
        'expns_misc_steam': 'opex_misc_steam',
        'tot_prdctn_expns': 'opex_production_total',
        'expns_per_mwh': 'opex_per_mwh'},
        inplace=True)

    # Now we need to assign IDs to the large steam plants, since FERC doesn't
    # do this for us.
    if verbose:
        print("        Identifying distinct large FERC plants.")

    # scikit-learn still doesn't deal well with NA values (this will be fixed
    # eventually) We need to massage the type and missing data for the
    # Classifier to work.
    ferc1_steam_df['construction_year'] = \
        pudl.transform.pudl.fix_int_na(ferc1_steam_df.construction_year)

    # Train the classifier
    ferc_clf = pudl.transform.ferc1.make_ferc_clf(
        ferc1_steam_df,
        ngram_min=2,
        ngram_max=10,
        min_sim=0.75,
        plant_name_wt=2.0,
        plant_type_wt=2.0,
        construction_type_wt=1.0,
        capacity_mw_wt=1.0,
        construction_year_wt=1.0,
        utility_id_ferc_wt=1.0)
    ferc_clf = ferc_clf.fit_transform(ferc1_steam_df)

    # Use the classifier to generate groupings of similar records:
    record_groups = ferc_clf.predict(ferc1_steam_df.record_id)
    n_tot = len(ferc1_steam_df)
    n_grp = len(record_groups)
    pct_grp = n_grp / n_tot

    # We only need one copy of each record group:
    record_groups = record_groups.drop_duplicates()
    record_groups = record_groups.reset_index(drop=True)
    if verbose:
        print(f"        {len(record_groups)} large FERC plants identified.")
        print(f"        {n_grp} of {n_tot} records ({pct_grp}) categorized.")
    record_groups = record_groups.stack().reset_index(level=1, drop=True)

    # Get rid of empty records
    record_groups = record_groups[record_groups != '']

    # Merge the plant IDs into the plants table on record_id
    record_groups.name = 'record_id'
    record_groups.index.name = 'plant_id_ferc1'
    ferc_plant_ids = record_groups.reset_index()
    ferc1_steam_df = pd.merge(ferc1_steam_df, ferc_plant_ids, on='record_id')

    # Set the construction year back to numeric because it is.
    ferc1_steam_df['construction_year'] = pd.to_numeric(
        ferc1_steam_df['construction_year'], errors='coerce')

    ferc1_transformed_dfs['plants_steam_ferc1'] = ferc1_steam_df

    return ferc1_transformed_dfs


def plants_small(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 plant_small data for loading into PUDL Database.

    This FERC Form 1 table contains information about a large number of small
    plants, including many small hydroelectric and other renewable generation
    facilities. Unfortunately the data is not well standardized, and so the
    plants have been categorized manually, with the results of that
    categorization stored in an Excel spreadsheet. This function reads in the
    plant type data from the spreadsheet and merges it with the rest of the
    information from the FERC DB based on record number, FERC respondent ID,
    and report year. When possible the FERC license number for small hydro
    plants is also manually extracted from the data.

    This categorization will need to be renewed with each additional year of
    FERC data we pull in. As of v0.1 the small plants have been categorized
    for 2004-2015.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_small_df = ferc1_raw_dfs['plants_small_ferc1']
    # Standardize plant_name_raw capitalization and remove leading/trailing
    # white space -- necesary b/c plant_name_raw is part of many foreign keys.
    ferc1_small_df = pudl.helpers.strip_lower(
        ferc1_small_df, ['plant_name', 'kind_of_fuel']
    )

    # Force the construction and installation years to be numeric values, and
    # set them to NA if they can't be converted. (table has some junk values)
    ferc1_small_df['yr_constructed'] = pd.to_numeric(
        ferc1_small_df['yr_constructed'],
        errors='coerce')
    # Convert from cents per mmbtu to dollars per mmbtu to be consistent
    # with the f1_fuel table data. Also, let's use a clearer name.
    ferc1_small_df['fuel_cost_per_mmbtu'] = ferc1_small_df['fuel_cost'] / 100.0
    ferc1_small_df.drop('fuel_cost', axis=1, inplace=True)

    # Create a single "record number" for the individual lines in the FERC
    # Form 1 that report different small plants, so that we can more easily
    # tell whether they are adjacent to each other in the reporting.
    ferc1_small_df['record_number'] = 46 * ferc1_small_df['spplmnt_num'] + \
        ferc1_small_df['row_number']

    # Unforunately the plant types were not able to be parsed automatically
    # in this table. It's been done manually for 2004-2015, and the results
    # get merged in in the following section.
    small_types_file = os.path.join(SETTINGS['pudl_dir'],
                                    'results',
                                    'ferc1_small_plants',
                                    'small_plants_2004-2016.xlsx')
    small_types_df = pd.read_excel(small_types_file)

    # Only rows with plant_type set will give us novel information.
    small_types_df.dropna(subset=['plant_type', ], inplace=True)
    # We only need this small subset of the columns to extract the plant type.
    small_types_df = small_types_df[['report_year', 'respondent_id',
                                     'record_number', 'plant_name_clean',
                                     'plant_type', 'ferc_license']]

    # Munge the two dataframes together, keeping everything from the
    # frame we pulled out of the FERC1 DB, and supplementing it with the
    # plant_name, plant_type, and ferc_license fields from our hand
    # made file.
    ferc1_small_df = pd.merge(ferc1_small_df,
                              small_types_df,
                              how='left',
                              on=['report_year',
                                  'respondent_id',
                                  'record_number'])

    # Remove extraneous columns and add a record ID
    ferc1_small_df = _clean_cols(ferc1_small_df)

    # Standardize plant_name capitalization and remove leading/trailing white
    # space, so that plant_name matches formatting of plant_name_raw
    ferc1_small_df = pudl.helpers.strip_lower(
        ferc1_small_df, ['plant_name_clean']
    )

    # in order to create one complete column of plant names, we have to use the
    # cleaned plant names when available and the orignial plant names when the
    # cleaned version is not available, but the strings first need cleaning
    ferc1_small_df['plant_name_clean'] = \
        ferc1_small_df['plant_name_clean'].fillna(value="")
    ferc1_small_df['plant_name_clean'] = ferc1_small_df.apply(
        lambda row: row['plant_name']
        if (row['plant_name_clean'] == "")
        else row['plant_name_clean'],
        axis=1)

    # now we don't need the uncleaned version anymore
    # ferc1_small_df.drop(['plant_name'], axis=1, inplace=True)

    ferc1_small_df.rename(columns={
        # FERC 1 DB Name      PUDL DB Name
        'respondent_id': 'utility_id_ferc',
        'plant_name': 'plant_name_original',
        'plant_name_clean': 'plant_name',
        'ferc_license': 'ferc_license_id',
        'yr_constructed': 'construction_year',
        'capacity_rating': 'capacity_mw',
        'net_demand': 'peak_demand_mw',
        'net_generation': 'net_generation_mwh',
        'plant_cost': 'total_cost_of_plant',
        'plant_cost_mw': 'capex_per_mw',
        'operation': 'opex_total',
        'expns_fuel': 'opex_fuel',
        'expns_maint': 'opex_maintenance',
        'kind_of_fuel': 'fuel_type',
        'fuel_cost': 'fuel_cost_per_mmbtu'},
        inplace=True)

    ferc1_transformed_dfs['plants_small_ferc1'] = ferc1_small_df

    return ferc1_transformed_dfs


def plants_hydro(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 plant_hydro data for loading into PUDL Database.

    Standardizes plant names (stripping whitespace and Using Title Case).  Also
    converts into our preferred units of MW and MWh.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_hydro_df = _clean_cols(ferc1_raw_dfs['plants_hydro_ferc1'])

    # Standardize plant_name capitalization and remove leading/trailing white
    # space -- necesary b/c plant_name is part of many foreign keys.
    ferc1_hydro_df = pudl.helpers.strip_lower(ferc1_hydro_df, ['plant_name'])

    # Converting kWh to MWh
    ferc1_hydro_df['net_generation_mwh'] = \
        ferc1_hydro_df['net_generation'] / 1000.0
    ferc1_hydro_df.drop('net_generation', axis=1, inplace=True)
    # Converting cost per kW installed to cost per MW installed:
    ferc1_hydro_df['cost_per_mw'] = ferc1_hydro_df['cost_per_kw'] * 1000.0
    ferc1_hydro_df.drop('cost_per_kw', axis=1, inplace=True)
    # Converting kWh to MWh
    ferc1_hydro_df['expns_per_mwh'] = ferc1_hydro_df['expns_kwh'] * 1000.0
    ferc1_hydro_df.drop('expns_kwh', axis=1, inplace=True)

    ferc1_hydro_df['yr_const'] = pd.to_numeric(
        ferc1_hydro_df['yr_const'],
        errors='coerce')
    ferc1_hydro_df['yr_installed'] = pd.to_numeric(
        ferc1_hydro_df['yr_installed'],
        errors='coerce')
    ferc1_hydro_df.dropna(inplace=True)
    ferc1_hydro_df.rename(columns={
        # FERC1 DB          PUDL DB
        'respondent_id': 'utility_id_ferc',
        'project_no': 'project_num',
        'yr_const': 'construction_year',
        'plant_kind': 'plant_type',
        'plant_const': 'plant_construction_type',
        'yr_installed': 'installation_year',
        'tot_capacity': 'capacity_mw',
        'peak_demand': 'peak_demand_mw',
        'plant_hours': 'plant_hours_connected_while_generating',
        'favorable_cond': 'net_capacity_favorable_conditions_mw',
        'adverse_cond': 'net_capacity_adverse_conditions_mw',
        'avg_num_of_emp': 'avg_num_employees',
        'cost_of_land': 'capex_land',
        'cost_structure': 'capex_structure',
        'cost_facilities': 'capex_facilities',
        'cost_equipment': 'capex_equipment',
        'cost_roads': 'capex_roads',
        'cost_plant_total': 'capex_total',
        'cost_per_mw': 'capex_per_mw',
        'expns_operations': 'opex_operations',
        'expns_water_pwr': 'opex_water_pwr',
        'expns_hydraulic': 'opex_hydraulic',
        'expns_electric': 'opex_electric',
        'expns_generation': 'opex_generation',
        'expns_rents': 'opex_rents',
        'expns_engineering': 'opex_engineering',
        'expns_structures': 'opex_structures',
        'expns_dams': 'opex_dams',
        'expns_plant': 'opex_plant',
        'expns_misc_plant': 'opex_misc_plant',
        'expns_per_mwh': 'opex_per_mwh',
        'expns_engnr': 'opex_engineering',
        'expns_total': 'opex_total',
        'asset_retire_cost': 'asset_retirement_cost',
        '': '',

    }, inplace=True)

    ferc1_transformed_dfs['plants_hydro_ferc1'] = ferc1_hydro_df

    return ferc1_transformed_dfs


def plants_pumped_storage(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 pumped storage data for loading into PUDL Database.

    Standardizes plant names (stripping whitespace and Using Title Case).  Also
    converts into our preferred units of MW and MWh.

    Args:
    -----
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns:
    --------
        transformed dataframe.

    """
    # grab table from dictionary of dfs
    ferc1_pumped_storage_df = _clean_cols(
        ferc1_raw_dfs['plants_pumped_storage_ferc1'])

    # Standardize plant_name capitalization and remove leading/trailing white
    # space -- necesary b/c plant_name is part of many foreign keys.
    ferc1_pumped_storage_df = pudl.helpers.strip_lower(
        ferc1_pumped_storage_df, ['plant_name']
    )

    # Converting kWh to MWh
    ferc1_pumped_storage_df['net_generation_mwh'] = \
        ferc1_pumped_storage_df['net_generation'] / 1000.0
    ferc1_pumped_storage_df.drop('net_generation', axis=1, inplace=True)

    ferc1_pumped_storage_df['energy_used_for_pumping_mwh'] = \
        ferc1_pumped_storage_df['energy_used'] / 1000.0
    ferc1_pumped_storage_df.drop('energy_used', axis=1, inplace=True)

    ferc1_pumped_storage_df['net_load_mwh'] = \
        ferc1_pumped_storage_df['net_load'] / 1000.0
    ferc1_pumped_storage_df.drop('net_load', axis=1, inplace=True)

    # Converting cost per kW installed to cost per MW installed:
    ferc1_pumped_storage_df['cost_per_mw'] = \
        ferc1_pumped_storage_df['cost_per_kw'] * 1000.0
    ferc1_pumped_storage_df.drop('cost_per_kw', axis=1, inplace=True)

    ferc1_pumped_storage_df['expns_per_mwh'] = \
        ferc1_pumped_storage_df['expns_kwh'] * 1000.0
    ferc1_pumped_storage_df.drop('expns_kwh', axis=1, inplace=True)

    ferc1_pumped_storage_df['yr_const'] = pd.to_numeric(
        ferc1_pumped_storage_df['yr_const'],
        errors='coerce')
    ferc1_pumped_storage_df['yr_installed'] = pd.to_numeric(
        ferc1_pumped_storage_df['yr_installed'],
        errors='coerce')

    ferc1_pumped_storage_df.dropna(inplace=True)

    ferc1_pumped_storage_df.rename(columns={
        # FERC1 DB          PUDL DB
        'respondent_id': 'utility_id_ferc',
        'project_number': 'project_num',
        'tot_capacity': 'capacity_mw',
        'project_no': 'project_num',
        'peak_demand': 'peak_demand_mw',
        'yr_const': 'construction_year',
        'yr_installed': 'installation_year',
        'plant_hours': 'plant_hours_connected_while_generating',
        'plant_capability': 'plant_capability_mw',
        'avg_num_of_emp': 'avg_num_employees',
        'cost_wheels': 'capex_wheels_turbines_generators',
        'cost_land': 'capex_land',
        'cost_structures': 'capex_structures',
        'cost_facilties': 'capex_facilities',
        'cost_wheels_turbines_generators': 'capex_wheels_turbines_generators',
        'cost_electric': 'capex_equipment_electric',
        'cost_misc_eqpmnt': 'capex_equipment_misc',
        'cost_roads': 'capex_roads',
        'asset_retire_cost': 'asset_retirement_cost',
        'cost_of_plant': 'capex_plant_total',
        'cost_per_mw': 'capex_per_mw',
        'expns_operations': 'opex_operations',
        'expns_water_pwr': 'opex_water_for_power',
        'expns_pump_strg': 'opex_pumped_storage',
        'expns_electric': 'opex_electric',
        'expns_misc_power': 'opex_generation_misc',
        'expns_rents': 'opex_rents',
        'expns_engneering': 'opex_engineering',
        'expns_structures': 'opex_structures',
        'expns_dams': 'opex_dams',
        'expns_plant': 'opex_plant',
        'expns_misc_plnt': 'opex_plant_misc',
        'expns_producton': 'opex_production_before_pumping',
        'pumping_expenses': 'opex_pumping',
        'tot_prdctn_exns': 'opex_total',
        'expns_per_mwh': 'opex_per_mwh'},
        inplace=True)

    ferc1_transformed_dfs['plants_pumped_storage_ferc1'] = \
        ferc1_pumped_storage_df

    return ferc1_transformed_dfs


def plant_in_service(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 plant_in_service data for loading into PUDL Database.

    This information is organized by FERC account, with each line of the FERC
    Form 1 having a different FERC account id (most are numeric and correspond
    to FERC's Uniform Electric System of Accounts). As of PUDL v0.1, this data
    is only valid from 2007 onward, as the line numbers for several accounts
    are different in earlier years.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_pis_df = ferc1_raw_dfs['plant_in_service_ferc1']

    # Now we need to add a column to the DataFrame that has the FERC account
    # IDs corresponding to the row_number that's already in there...
    ferc_accts_df = pc.ferc_electric_plant_accounts.drop(
        ['ferc_account_description'], axis=1)
    ferc_accts_df.dropna(inplace=True)
    ferc_accts_df['row_number'] = ferc_accts_df['row_number'].astype(int)

    ferc1_pis_df = pd.merge(ferc1_pis_df, ferc_accts_df,
                            how='left', on='row_number')
    ferc1_pis_df = _clean_cols(ferc1_pis_df)

    ferc1_pis_df.rename(columns={
        # FERC 1 DB Name  PUDL DB Name
        'respondent_id': 'utility_id_ferc',
        'begin_yr_bal': 'beginning_year_balance',
        'addition': 'additions',
        'yr_end_bal': 'year_end_balance'},
        inplace=True)

    ferc1_transformed_dfs['plant_in_service_ferc1'] = ferc1_pis_df

    return ferc1_transformed_dfs


def purchased_power(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 pumped storage data for loading into PUDL Database.

    This table has data about inter-untility power purchases into the PUDL DB.
    This includes how much electricty was purchased, how much it cost, and who
    it was purchased from. Unfortunately the field describing which other
    utility the power was being bought from is poorly standardized, making it
    difficult to correlate with other data. It will need to be categorized by
    hand or with some fuzzy matching eventually.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_purchased_pwr_df = _clean_cols(
        ferc1_raw_dfs['purchased_power_ferc1'])

    ferc1_purchased_pwr_df.replace(to_replace='', value=np.nan, inplace=True)
    ferc1_purchased_pwr_df.dropna(subset=['sttstcl_clssfctn',
                                          'rtsched_trffnbr'], inplace=True)

    ferc1_purchased_pwr_df.rename(columns={
        # FERC 1 DB Name  PUDL DB Name
        'respondent_id': 'utility_id_ferc',
        'athrty_co_name': 'authority_company_name',
        'sttstcl_clssfctn': 'statistical_classification',
        'rtsched_trffnbr': 'rate_schedule_tariff_num',
        'avgmth_bill_dmnd': 'avg_billing_demand_mw',
        'avgmth_ncp_dmnd': 'avg_monthly_ncp_demand_mw',
        'avgmth_cp_dmnd': 'avg_monthly_cp_demand_mw',
        'mwh_purchased': 'purchased_mwh',
        'mwh_recv': 'received_mwh',
        'mwh_delvd': 'delivered_mwh',
        'dmnd_charges': 'demand_charges',
        'erg_charges': 'energy_charges',
        'othr_charges': 'other_charges',
        'settlement_tot': 'total_settlement'},
        inplace=True)

    ferc1_transformed_dfs['purchased_power_ferc1'] = ferc1_purchased_pwr_df

    return ferc1_transformed_dfs


def accumulated_depreciation(ferc1_raw_dfs, ferc1_transformed_dfs, verbose=True):
    """
    Transform FERC Form 1 depreciation data for loading into PUDL Database.

    This information is organized by FERC account, with each line of the FERC
    Form 1 having a different descriptive identifier like 'balance_end_of_year'
    or 'transmission'.

    Args:
        ferc1_raw_dfs (dictionary of pandas.DataFrame): Each entry in this
            dictionary of DataFrame objects corresponds to a page from the
            EIA860 form, as reported in the Excel spreadsheets they distribute.
        ferc1_transformed_dfs (dictionary of DataFrames)

    Returns: transformed dataframe.
    """
    # grab table from dictionary of dfs
    ferc1_apd_df = ferc1_raw_dfs['accumulated_depreciation_ferc1']

    ferc1_acct_apd = pc.ferc_accumulated_depreciation.drop(
        ['ferc_account_description'], axis=1)
    ferc1_acct_apd.dropna(inplace=True)
    ferc1_acct_apd['row_number'] = ferc1_acct_apd['row_number'].astype(int)

    ferc1_accumdepr_prvsn_df = pd.merge(ferc1_apd_df, ferc1_acct_apd,
                                        how='left', on='row_number')
    ferc1_accumdepr_prvsn_df = _clean_cols(ferc1_accumdepr_prvsn_df)

    ferc1_accumdepr_prvsn_df.rename(columns={
        # FERC1 DB   PUDL DB
        'respondent_id': 'utility_id_ferc',
        'total_cde': 'total'},
        inplace=True)

    ferc1_transformed_dfs['accumulated_depreciation_ferc1'] = \
        ferc1_accumdepr_prvsn_df

    return ferc1_transformed_dfs


def transform(ferc1_raw_dfs,
              ferc1_tables=pc.ferc1_pudl_tables,
              verbose=True):
    """Transform FERC 1."""
    ferc1_transform_functions = {
        'fuel_ferc1': fuel,
        'plants_steam_ferc1': plants_steam,
        'plants_small_ferc1': plants_small,
        'plants_hydro_ferc1': plants_hydro,
        'plants_pumped_storage_ferc1': plants_pumped_storage,
        'plant_in_service_ferc1': plant_in_service,
        'purchased_power_ferc1': purchased_power,
        'accumulated_depreciation_ferc1': accumulated_depreciation
    }
    # create an empty ditctionary to fill up through the transform fuctions
    ferc1_transformed_dfs = {}

    # for each ferc table,
    if verbose:
        print("Transforming dataframes from FERC 1:")
    for table in ferc1_transform_functions:
        if table in ferc1_tables:
            if verbose:
                print("    {}...".format(table))
            ferc1_transform_functions[table](ferc1_raw_dfs,
                                             ferc1_transformed_dfs,
                                             verbose=verbose)

    return ferc1_transformed_dfs

###############################################################################
# Identifying FERC Plants
###############################################################################
# Sadly FERC doesn't provide any kind of real IDs for the plants that report to
# them -- all we have is their names (a freeform string) and the data that is
# reported alongside them. This is often enough information to be able to
# recognize which records ought to be associated with each other year to year
# to create a continuous time series. However, we want to do that
# programmatically, which means using some clustering / categorization tools
# from scikit-learn


class FERCPlantClassifier(BaseEstimator, ClassifierMixin):
    """
    A classifier for identifying FERC plant time series in FERC Form 1 data.

    We want to be able to give the classifier a FERC plant record, and get back
    the group of records(or the ID of the group of records) that it ought to
    be part of.

    There are hundreds of different groups of records, and we can only know
    what they are by looking at the whole dataset ahead of time. This is the
    "fitting" step, in which the groups of records resulting from a particular
    set of model parameters(e.g. the weights that are attributes of the class)
    are generated.

    Once we have that set of record categories, we can test how well the
    classifier performs, by checking it against test / training data which we
    have already classified by hand. The test / training set is a list of lists
    of unique FERC plant record IDs(each record ID is the concatenation of:
    report year, respondent id, supplement number, and row number). It could
    also be stored as a dataframe where each column is associated with a year
    of data(some of which could be empty). Not sure what the best structure
    would be.

    If it's useful, we can assign each group a unique ID that is the time
    ordered concatenation of each of the constituent record IDs. Need to
    understand what the process for checking the classification of an input
    record looks like.

    To score a given classifier, we can look at what proportion of the records
    in the test dataset are assigned to the same group as in our manual
    classification of those records. There are much more complicated ways to
    do the scoring too... but for now let's just keep it as simple as possible.

    """

    def __init__(self, min_sim=0.75, plants_df=None):
        """
        Initialize the classifier.

        Parameters:
        -----------
        min_sim : Number between 0.0 and 1.0, indicating the minimum value of
            cosine similarity that we are willing to accept as indicating two
            records are part of the same plant record time series. All entries
            in the pairwise similarity matrix below this value will be zeroed
            out.
        plants_df : The entire FERC Form 1 plant table as a dataframe. Needed
            in order to calculate the distance metrics between all of the
            records so we can group the plants in the fit() step, so we can
            check how well they are categorized later...

        """
        self.min_sim = min_sim
        self.plants_df = plants_df
        self._years = self.plants_df.report_year.unique()

    def fit(self, X, y=None):
        """
        The fit method takes the vectorized, normalized, weighted FERC plant
        features (X) as input, calculates the pairwise cosine similarity matrix
        between all records, and groups the records in their best time series.
        The similarity matrix and best time series are stored as data members
        in the object for later use in scoring & predicting.

        This isn't quite the way a fit method would normally work.

        Args:
        -----
            X: a sparse matrix of size n_samples x n_features.

        Returns:
        --------
            self

        """
        self._cossim_df = pd.DataFrame(cosine_similarity(X))
        self._best_of = self._best_by_year()
        # Make the best match indices integers rather than floats w/ NA values.
        self._best_of[self._years] = \
            self._best_of[self._years].fillna(-1).astype(int)

        return self

    def transform(self, X, y=None):
        return self

    def predict(self, X, y=None):
        """
        Given a one-dimensional dataframe X, containing FERC record IDs,
        return a dataframe in which each row corresponds to one of the input
        record_id values (ordered as the input was ordered), with each column
        corresponding to one of the years worth of data. Values in the returned
        dataframe are the FERC record_ids of the record most similar to the
        input record within that year. Some of them may be null, if there was
        no sufficiently good match.

        Row index is the seed record IDs. Column index is years.

        TODO:
        ----------
        * This method is hideously inefficient. It should be vectorized.
        * There's a line that throws a FutureWarning that needs to be fixed.

        """
        try:
            getattr(self, "_cossim_df")
        except AttributeError:
            raise RuntimeError(
                "You must train classifer before predicting data!")

        out_df = pd.DataFrame(columns=self._years)
        out_idx = []
        # For each record_id we've been given:
        for x in X:
            # Find the index associated with the record IDs:
            idx = self._best_of[self._best_of.record_id == x].index.values[0]

            # Lookup which rows contain matches to that index:
            w_m = self._best_of[self._years][
                self._best_of[self._years] == idx]
            w_m = w_m.dropna(how='all').index.values
            b_m = self._best_of.loc[idx, self._years].astype(int)

            # Make sure that w_m and b_m satisfy whatever constraints we want
            # to impose on them. Some options include:
            # - Impose no constraints. (allow records to be used more than once
            #   (sometimes appropriate given ownership changes in plants)
            # - Require them to be identical, which means requiring that
            #   each record only appears in a single time series. Results in
            #   loss of some otherwise valid time series, but means the
            #   selection of best fits is deterministic.
            # - Once a record is used, remove it from the pool so it can't be
            #   used again, which means the next-most-similar record takes its
            #   place.
            if np.array_equiv(w_m, b_m[b_m >= 0].values):
                # This line is causing a warning. In cases where there are
                # some years no sufficiently good match exists, and so b_m
                # doesn't contain an index. Instead, it has a -1 sentinel
                # value, which isn't a label for which a record exists, which
                # upsets .loc. Need to find some way around this... but for
                # now it does what we want. We could use .iloc instead, but
                # then the -1 sentinel value maps to the last entry in the
                # dataframe, which also isn't what we want.  Blargh.
                new_grp = self._best_of.loc[b_m, 'record_id']

                # reshape into row, rather than column,
                new_grp = new_grp.values.reshape(1, len(self._years))

                # Stack the new list of record_ids on our output DataFrame:
                new_grp = pd.DataFrame(new_grp, columns=self._years)

                out_df = pd.concat([out_df, new_grp])

                # Save the seed record_id for use in indexing the output:
                out_idx = out_idx + [self._best_of.loc[idx, 'record_id']]

        out_df['seed_id'] = out_idx
        out_df = out_df.set_index('seed_id')
        out_df = out_df.fillna('')
        return out_df

    def score(self, X, y=None):
        """
        Score a collection of FERC plant categorizations.

        Given:
            X: an n_samples x 1 pandas dataframe of FERC Form 1 record IDs.
            y: a dataframe of "ground truth" FERC Form 1 record groups,
               corresponding to the list record IDs in X

            - for every record ID in X, predict its record group and calculate
              a metric of similarity between the prediction and the "ground
              truth" group that was passed in for that value of X.
            - Return the average of all those similarity metrics as the score.
        """

        scores = []
        for true_group in y:
            true_group = str.split(true_group, sep=',')
            true_group = [s for s in true_group if s != '']
            predicted_groups = self.predict(pd.DataFrame(true_group))
            for rec_id in true_group:
                sm = SequenceMatcher(None, true_group,
                                     predicted_groups.loc[rec_id])
                scores = scores + [sm.ratio()]

        return np.mean(scores)

    def _best_by_year(self):
        """Find the best match for each plant record in each other year."""
        # only keep similarity matrix entries above our minimum threshold:
        out_df = self.plants_df.copy()
        sim_df = self._cossim_df[self._cossim_df >= self.min_sim]

        # Add a column for each of the years, in which we will store indices
        # of the records which best match the record in question:
        for yr in self._years:
            newcol = yr
            out_df[newcol] = -1

        # seed_yr is the year we are matching *from* -- we do the entire
        # matching process from each year, since it may not be symmetric:
        for seed_yr in self._years:
            seed_idx = self.plants_df.index[
                self.plants_df.report_year == seed_yr]
            # match_yr is all the other years, in which we are finding the best
            # match
            for match_yr in self._years:
                best_of_yr = match_yr
                match_idx = self.plants_df.index[
                    self.plants_df.report_year == match_yr]
                # For each record specified by seed_idx, obtain the index of
                # the record within match_idx that that is the most similar.
                best_idx = sim_df.iloc[seed_idx, match_idx].idxmax(axis=1)
                out_df.iloc[seed_idx,
                            out_df.columns.get_loc(best_of_yr)] = best_idx

        return out_df


def make_ferc_clf(plants_df,
                  ngram_min=2,
                  ngram_max=10,
                  min_sim=0.75,
                  plant_name_wt=2.0,
                  plant_type_wt=2.0,
                  construction_type_wt=1.0,
                  capacity_mw_wt=1.0,
                  construction_year_wt=1.0,
                  utility_id_ferc_wt=1.0):
    """Create a boilerplate FERC Plant Classifier."""

    ferc_pipe = Pipeline([
        ('preprocessor', ColumnTransformer(
            transformers=[
                ('plant_name', Pipeline([
                    ('tfidf', TfidfVectorizer(analyzer='char',
                                              ngram_range=(ngram_min,
                                                           ngram_max))),
                ]), 'plant_name'),

                ('plant_type', Pipeline([
                    ('onehot', OneHotEncoder()),
                ]), ['plant_type']),

                ('construction_type', Pipeline([
                    ('onehot', OneHotEncoder()),
                ]), ['construction_type']),

                ('capacity_mw', Pipeline([
                    ('scaler', RobustScaler()),
                    ('norm', Normalizer())
                ]), ['capacity_mw']),

                ('construction_year', Pipeline([
                    ('onehot', OneHotEncoder(categories='auto')),
                ]), ['construction_year']),

                ('utility_id_ferc', Pipeline([
                    ('onehot', OneHotEncoder(categories='auto')),
                ]), ['utility_id_ferc'])
            ],

            transformer_weights={
                'plant_name': plant_name_wt,
                'plant_type': plant_type_wt,
                'construction_type': construction_type_wt,
                'capacity_mw': capacity_mw_wt,
                'construction_year': construction_year_wt,
                'utility_id_ferc': utility_id_ferc_wt,
            })
         ),
        ('classifier', pudl.transform.ferc1.FERCPlantClassifier(
            min_sim=min_sim, plants_df=plants_df))
    ])
    return ferc_pipe
