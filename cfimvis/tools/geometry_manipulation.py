# geometry_manipulation.py
import duckdb
import ibis
from ibis import _
import rasterio
from rasterio.features import shapes
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape
from rasterio.session import AWSSession
from rio_tiler.errors import PointOutsideBounds
from rio_tiler.io import COGReader
import rasterio
import boto3
import numpy as np
from tqdm import tqdm
import os

def add_point_geo(database_path: str, table_name: str, lat_col_nam: str, long_col_name: str) -> None:
    """
    Adds a 'geometry' column to a specified table and populates it with points 
    created from latitude and longitude columns.

    Args:
        database_path (str): Path to the DuckDB database file.
        table_name (str): Name of the table to update.
        lat_col_nam (str): Name of the column containing latitude values.
        long_col_name (str): Name of the column containing longitude values.

    Output:
        - Adds a new 'geometry' column to the specified table, 
          populated with point geometries created from the latitude and longitude values.
    """
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')
    data_conn.raw_sql(
        f""" 
        ALTER TABLE {table_name} ADD COLUMN geometry GEOMETRY; 
        UPDATE {table_name} SET geometry = ST_Point({long_col_name}, {lat_col_nam}); 
        """
    )
    data_conn.con.close()
    return

def write_to_database(database_path: str, table_name: str, df: pd.DataFrame([])=None, df_path: str=None) -> None:
    """
    Writes data to a database. Accepts either a DataFrame or a file path.
    
    Args:
        database_path (str): Path to the database.
        table_name (str): Name of the table in the database.
        df (pd.DataFrame, optional): DataFrame to write to the database.
        df_path (str, optional): Path to a file containing the data.
    
    Raises:
        ValueError: If both `df` and `df_path` are provided or neither is provided.
    """
    if (df is None and df_path is None) or (df is not None and df_path is not None):
        raise ValueError("Provide either `df` or `df_path`, but not both.")
    
    # Connect to the database
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')

    # Handle DataFrames
    if df is not None and not isinstance(df, gpd.GeoDataFrame):
        data_conn.create_table(table_name, df, overwrite=True) 
        data_conn.con.close()
        return
    
    # Handle GeoDataFrame input by saving it to a temporary file
    temp_file = None
    if df is not None:
        directory = 'temp'
        if not os.path.exists(directory):
            os.makedirs(directory)
        temp_file = os.path.join(directory, 'temp.parquet')
        df.to_parquet(temp_file, index=False)
        del df
        df_path = temp_file  # Use the temp file as the input path

    # Create or replace the table using the provided or temporary path
    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE {table_name} AS
        SELECT * FROM '{df_path}'
        """
    )

    # Clean up temporary file and directory, if created
    if temp_file:
        os.remove(temp_file)
        os.rmdir(directory)
    
    # Close the database connection
    data_conn.con.close()
    return


def get_none_overlapping(dem_path: str, database_path: str, point_gdf_table: str) -> None:
    """
    Identifies points from a GeoDataFrame that do not overlap with a raster (DEM) and stores the result in a database.

    Args:
        dem_path (str): Path to the raster DEM file.
        database_path (str): Path to the DuckDB database file.
        point_gdf_table (str): Name of the table in the database containing the points (GeoDataFrame).

    Output:
        - A new 'points_outside_dem' table in the database containing points from the GeoDataFrame 
          that do not intersect with the DEM (raster).
        - Metadata about the CRS of the involved datasets stored in a 'metadata' table.
    """
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')

    # Vectorize raster
    with rasterio.open(dem_path) as src:
        raster_crs = src.crs
        mask = src.dataset_mask()
        # Extract shapes from the raster
        raster_polygons = [
            shape(geom) for geom, val in shapes(mask, transform=src.transform) if val > 0
        ]
    # Create a GeoDataFrame from raster polygons
    raster_gdf = gpd.GeoDataFrame(geometry=raster_polygons, crs=raster_crs)

    # Match crs
    points_gdf = data_conn.table(point_gdf_table).execute()
    points_gdf = points_gdf.set_crs(epsg=4326)
    if raster_gdf.crs != points_gdf.crs:
        points_gdf = points_gdf.to_crs(raster_gdf.crs)
    print(f"Computing under crs: {raster_gdf.crs}")

    # Store to database
    directory = 'temp'
    if not os.path.exists(directory):
        os.makedirs(directory)
    raster_temp_file = os.path.join(directory, 'dem_bounds.parquet')
    point_temp_file = os.path.join(directory, 'transformed_nodes.parquet')

    raster_gdf.to_parquet(raster_temp_file, index=False)
    points_gdf.to_parquet(point_temp_file, index=False)

    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE dem_bounds AS
        SELECT * FROM '{raster_temp_file}';
        CREATE OR REPLACE TABLE transformed_nodes AS
        SELECT * FROM '{point_temp_file}';
        """
    )
    os.remove(raster_temp_file)
    os.remove(point_temp_file)
    os.rmdir(directory)

    # Find all that fall outside
    data_conn.raw_sql(
        """
        CREATE OR REPLACE TABLE points_outside_dem AS
        SELECT tn.*
        FROM transformed_nodes AS tn
        LEFT JOIN dem_bounds AS db
        ON ST_Intersects(tn.geometry, db.geometry)
        WHERE db.geometry IS NULL;
        """
    )

    # Save crs info
    data_conn.raw_sql(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            table_name STRING,
            crs STRING
        )
        """
    )
    data_conn.raw_sql(
        f"""
        INSERT INTO metadata (table_name, crs)
        VALUES 
            ('points_outside_dem', '{raster_gdf.crs.to_string()}'),
            ('dem_bounds', '{raster_gdf.crs.to_string()}'),
            ('transformed_nodes', '{raster_gdf.crs.to_string()}');
        """
    )
    data_conn.con.close()
    return 

def extract_elevation(dem_path: str, database_path: str) -> gpd.GeoDataFrame([]):
    """
    Extracts elevation data from a raster DEM for points stored in a 'nodes' table and stores the results in a new table.

    Args:
        dem_path (str): Path to the DEM file (in Cloud Optimized GeoTIFF format).
        database_path (str): Path to the DuckDB database file.

    Output:
        - A new 'nodes_elevation' table in the database, containing the 'node_id', 'long', 'lat', 
          and 'elevation' values for each node, with elevation data extracted from the raster DEM.
        - The CRS of the points is checked and transformed to match the raster CRS if necessary.
    """
    data_conn = ibis.duckdb.connect(database_path)
    try:
        data_conn.raw_sql('LOAD spatial')
    except: 
        data_conn.raw_sql('INSTALL spatial')
        data_conn.raw_sql('LOAD spatial')
    point_gdf = data_conn.table("nodes").execute()
    point_gdf = point_gdf.set_crs('EPSG:4326')

    with COGReader(dem_path) as cog:
        raster_crs = cog.dataset.crs
        points_crs = point_gdf.crs
        if points_crs != raster_crs:
            print("CRS mismatch detected. Transforming points to match raster CRS.")
            point_gdf = point_gdf.to_crs(raster_crs)
            print(f"new CRS: {point_gdf.crs}")
        else:
            print("CRS match. No transformation required.")

        # Extract raster values for the transformed points
        coords = [(geom.x, geom.y) for geom in point_gdf["geometry"]]
        # Disregard nodes with no elevation data
        values = []
        for x, y in tqdm(coords):
            try:
                # Extract the value
                value = cog.point(x, y, coord_crs=raster_crs).array[0]
                values.append(float(value))
            except PointOutsideBounds:
                # Assign NaN if the point is outside the raster bounds
                values.append(np.nan)
    
    # Add the extracted raster values to the Ibis DuckDB table
    point_gdf["elevation"] = values
    # Transform back to the 4326 and save
    if point_gdf.crs.to_string() != 'EPSG:4326':
        point_gdf = point_gdf.to_crs('EPSG:4326')
    directory = 'temp'
    if not os.path.exists(directory):
        os.makedirs(directory)
    temp_file = os.path.join(directory, 'elevation.parquet')
    point_gdf.to_parquet(temp_file, index=False)
    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE nodes_elevation AS 
        SELECT node_id, long, lat, elevation FROM '{temp_file}'
        """
    )
    os.remove(temp_file)
    os.rmdir(directory)
    data_conn.con.close()
    return 

def mask_nodes(database_path: str, table_name:str, masked_table_name:str) -> None:
    """
    Creates a new table with nodes masked based on their inclusion in the 'masked_elements' table.

    Args:
        database_path (str): Path to the DuckDB database file.
        table_name (str): Name of the table containing the nodes to be masked.
        masked_table_name (str): Name of the new table where the masked nodes will be stored.

    Output:
        - A new table (specified by 'masked_table_name') in the database containing only the nodes 
          whose 'node_id' matches any 'node_id_1', 'node_id_2', or 'node_id_3' in the 'masked_elements' table.
    """
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')
    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE '{masked_table_name}' AS
        SELECT * FROM '{table_name}' AS n
        WHERE node_id IN (
            SELECT node_id_1 FROM masked_elements
            UNION
            SELECT node_id_2 FROM masked_elements
            UNION
            SELECT node_id_3 FROM masked_elements
        );
        """
    )
    data_conn.con.close()
    return

def add_elevation(database_path: str, table_name:str, elevation_table:str) -> None:
    """
    Adds elevation data from an elevation table to a node table based on matching node IDs.

    Args:
        database_path (str): Path to the DuckDB database file.
        table_name (str): Name of the table containing the nodes to which elevation data will be added.
        elevation_table (str): Name of the table containing elevation data.

    Output:
        - Updates the table specified by 'table_name' by adding the 'elevation' column 
          from the 'elevation_table' for each matching 'node_id'. 
        - Only nodes with non-null elevation values are retained in the resulting table.
    """
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')
    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE '{table_name}' AS
        SELECT n.node_id, n.long, n.lat, n.wse, e.elevation
        FROM '{table_name}' AS n
        LEFT JOIN '{elevation_table}' AS e
        ON n.node_id = e.node_id
        WHERE e.elevation IS NOT NULL;
        """
    )
    data_conn.con.close()
    return

