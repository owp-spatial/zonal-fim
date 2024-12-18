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

def add_point_geo(database_path: str, table_name: str, lat_col_nam: str, long_col_name: str):
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

def write_to_database(database_path: str, table_name: str, df: pd.DataFrame([])) -> None:
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')
    
    directory = 'temp'
    if not os.path.exists(directory):
        os.makedirs(directory)
    df_temp_file = os.path.join(directory, 'temp.parquet')
    df.to_parquet(df_temp_file, index=False)
    del df

    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE {table_name} AS
        SELECT * FROM '{df_temp_file}'
        """
    )
    os.remove(df_temp_file)
    os.rmdir(directory)
    data_conn.con.close()
    return

def get_none_overlapping(s3_path: str, database_path: str, point_gdf_table: str) -> None:
    data_conn = ibis.duckdb.connect(database_path)
    data_conn.raw_sql('LOAD spatial')

    # Vectorize raster
    with rasterio.open(s3_path) as src:
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

def extract_elevation(s3_path: str, database_path: str) -> gpd.GeoDataFrame([]):

    data_conn = ibis.duckdb.connect(database_path)
    try:
        data_conn.raw_sql('LOAD spatial')
    except: 
        data_conn.raw_sql('INSTALL spatial')
        data_conn.raw_sql('LOAD spatial')
    point_gdf = data_conn.table("nodes").execute()
    point_gdf = point_gdf.set_crs('EPSG:4326')

    with COGReader(s3_path) as cog:
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
    data_conn.raw_sql(f"""
                        CREATE OR REPLACE TABLE nodes_elevation AS 
                        SELECT * FROM '{temp_file}'
                        """)
    os.remove(temp_file)
    os.rmdir(directory)
    data_conn.con.close()
    return 

