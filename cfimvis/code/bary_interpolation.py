# bary_interpolation.py

import duckdb
import ibis
from ibis import _
import rasterio
import pandas as pd
import numpy as np

def interpolate(database_path: str, output_database_path: str, s3_path: str, save_raster: bool=False) -> None:
    """
    The `interpolate` function processes spatial data to generate a raster file representing 
    water surface elevation (WSE) using barycentric interpolation. It produces 
    a GeoTIFF file as output.

    Input:
        - database_path (str): 
            The file path to the DuckDB database containing required spatial data tables:
              * `triangle_barycentric`: Includes `pg_id` and `wse_weighted_average`.
              * `z_w`: Maps cells to polygons with `pg_id`, `cell`, and `coverage_fraction`.

        - s3_path (str): 
            The path to a reference GeoTIFF file on the local file system or an S3-compatible 
            storage. This file provides raster dimensions and metadata.

    Output:
        - None: 
            Generates a GeoTIFF raster file (`wse_barycentric_interpolation.tif`) containing 
            interpolated WSE values.

    Example:
        1. Ensure the following data exists:
           - DuckDB database (`my_database.duckdb`) with `triangle_barycentric` and `z_w` tables.
           - Reference raster file (`DEM_masked_4326.tif`).

        2. Call the function:
            interpolate("my_database.duckdb", "data/DEM_masked_4326.tif")

        3. Result:
            A GeoTIFF file named `wse_barycentric_interpolation.tif` is created in the `data/` 
            directory, containing interpolated WSE values.

    Notes:
        - The output file uses `float32` data type for raster values.

    """
    data_conn = ibis.duckdb.connect(database_path)
    out_data_conn = ibis.duckdb.connect(output_database_path)
    for conn in [data_conn, out_data_conn]:
        try:
            conn.raw_sql('LOAD spatial')
        except:
            conn.raw_sql('INSTALL spatial')
            conn.raw_sql('LOAD spatial')

    out_data_conn.raw_sql(f"ATTACH '{database_path}' AS compute_db;")

    merged_polys = out_data_conn.table("triangle_barycentric").select(['pg_id', 'wse_weighted_average'])
    z_w = data_conn.table("coverage_fraction").select(['pg_id', 'cell', 'coverage_fraction', 'elevation'])      
    
    # left join on the 'pg_id' column
    z_w_merged = z_w.join(merged_polys, z_w.pg_id == merged_polys.pg_id, how='left')
    z_w_merged = z_w_merged.drop(z_w_merged.pg_id_right)
    grouped = z_w_merged.group_by("cell")

    # Calculate 'wse_cell_weighted_average'
    numerator = grouped.aggregate(
        weighted_sum = (z_w_merged.wse_weighted_average * z_w_merged.coverage_fraction).sum()
    )
    denominator = grouped.aggregate(
        coverage_sum = z_w_merged.coverage_fraction.sum()
    )
    z_w_merged = z_w_merged.left_join(numerator, z_w_merged.cell == numerator.cell)
    z_w_merged = z_w_merged.drop(z_w_merged.cell_right)
    z_w_merged = z_w_merged.left_join(denominator, z_w_merged.cell == denominator.cell)
    z_w_merged = z_w_merged.drop(z_w_merged.cell_right)
    z_w_merged = z_w_merged.mutate(
        wse_cell_weighted_average=z_w_merged.weighted_sum / z_w_merged.coverage_sum
    )

    z_w_merged = z_w_merged.group_by('cell').aggregate(
        wse_cell_weighted_average=z_w_merged.wse_cell_weighted_average.first()
    )

    z_w_cell = (
        z_w
        .group_by("cell")  
        .aggregate(*[z_w[col].first().name(col) for col in z_w.columns if col != "cell"])  # Aggregate all columns except 'cell'
    )

    z_w_merged = z_w_merged.join(z_w_cell.select(['cell', 'elevation']), z_w_merged.cell == z_w_cell.cell, how='left')
    z_w_merged = z_w_merged.drop(z_w_merged.cell_right)
    z_w_merged = z_w_merged.filter(~z_w_merged["elevation"].isnan())

    z_w_with_depth = z_w_merged.mutate(depth=z_w_merged["wse_cell_weighted_average"] - z_w_merged["elevation"])

    # Filter the rows where 'depth' is greater than or equal to 0
    filtered_z_w = z_w_with_depth.filter(z_w_with_depth["depth"] >= 0)
    out_data_conn.create_table("depth", filtered_z_w,  overwrite=True)

    if save_raster:

        # Load DEM 
        with rasterio.open(s3_path) as src:
            raster_meta = src.meta
            total_pixels = raster_meta['width'] * raster_meta['height']
            width = raster_meta['width']
            height = raster_meta['height']
        
        df = pd.DataFrame({'cell': range(1, total_pixels + 1), 'wse_cell_weighted_average': 0})
        data_conn.register(df, 'cell_range_table') 
        cell_range_table = data_conn.table('cell_range_table')

    # Assuming z_w_merged is already an Ibis table
    z_w_merged_with_missing = cell_range_table.left_join(
        z_w_merged,
        cell_range_table['cell'] == z_w_merged['cell']
    ).mutate(
        wse_cell_weighted_average=ibis.coalesce(
            z_w_merged['wse_cell_weighted_average'],
            cell_range_table['wse_cell_weighted_average']
        )
    ).select(
        'cell',  
        'wse_cell_weighted_average'  
    )
    z_w_merged_with_missing = z_w_merged_with_missing.order_by('cell')

    # Write as a raster file
    df_complete = z_w_merged_with_missing.execute()
    # Extract the column with cell indices and the values
    cell_indices = df_complete.index.to_numpy()  
    values = df_complete['wse_cell_weighted_average'].to_numpy()

    row_indices = ((cell_indices - 1) // width).astype(int) 
    col_indices = ((cell_indices - 1) % width).astype(int)   

    raster_array = np.zeros((height, width))
    raster_array[row_indices, col_indices] = values

    # Read metadata from the existing GeoTIFF file (DEM file)
    with rasterio.open(s3_path) as src:
        raster_meta = src.meta.copy()

    raster_meta.update({
        'dtype': 'float32',  
        'count': 1,          
        'compress': 'deflate',   
    })

    output_path = "data/wse_barycentric_interpolation.tif"
    with rasterio.open(output_path, 'w', **raster_meta) as dst:
        dst.write(raster_array.astype('float32'), 1)
    data_conn.con.close()
    out_data_conn.con.close()
    return

def make_depth_raster(dem_path: str, wse_path: str = "data/wse_barycentric_interpolation.tif", 
                      mask_negative: bool = True) -> None:
    """
    The `make_depth_raster` function calculates a depth raster by computing the difference between 
    a water surface elevation (WSE) raster and a digital elevation model (DEM) raster. The result 
    is saved as a new GeoTIFF file.

    Input:
        - dem_path (str): 
            File path to the DEM raster file. This file represents the elevation of the terrain.

        - wse_path (str, optional): 
            File path to the WSE raster file. The default value is 
            "data/wse_barycentric_interpolation.tif". This raster represents water surface elevation.

        - mask_negative (bool, optional): 
            If True, negative values in the resulting depth raster are masked (set to `NaN`). 
            Default is True.

    Output:
        - None: 
            Generates a GeoTIFF raster file (`depth_barycentric_interpolation.tif`) in the `data/` 
            directory, representing the depth values.

    Example:
        1. Ensure the following input files are available:
           - A DEM raster file, e.g., `data/DEM_masked_4326.tif`.
           - A WSE raster file, e.g., `data/wse_barycentric_interpolation.tif`.

        2. Call the function:
            make_depth_raster("data/DEM_masked_4326.tif", "data/wse_barycentric_interpolation.tif")

        3. Result:
            A GeoTIFF file named `depth_barycentric_interpolation.tif` is created in the `data/` 
            directory, containing the depth values.

    Notes:
        - Both input rasters must have the same Coordinate Reference System (CRS). 
          If they differ, the function raises a `ValueError`.
        - NoData values in either raster are preserved in the output raster as `NaN`.
    """
    
    with rasterio.open(wse_path) as src1:
        wse_values = src1.read(1)  # Read the first band of raster1
        raster1_transform = src1.transform
        raster1_nodata = src1.nodata

        # Open the larger raster (raster2) and align it to the extent of raster1
        with rasterio.open(dem_path) as src2:
            # Ensure both rasters have the same CRS
            if src1.crs != src2.crs:
                print(src1.crs)
                print(src2.crs)
                raise ValueError("The CRS of both rasters must match.")
            
            # Window of raster2 to match raster1
            window = rasterio.windows.from_bounds(
                *src1.bounds, transform=src2.transform
            )
            
            # Read the data from raster2 using the window
            dem_values = src2.read(1, window=window, out_shape=wse_values.shape)
            raster2_nodata = src2.nodata

        # Calculate the difference
        difference = wse_values - dem_values

        # Handle NoData values (optional, depends on your data)
        nodata_value = raster1_nodata if raster1_nodata is not None else raster2_nodata
        if nodata_value is not None:
            difference[(wse_values == nodata_value) | (dem_values == nodata_value)] = np.nan
            
        if mask_negative:
            difference[difference<=0.0] = np.nan
        # Save the result as a new raster file
        profile = src1.profile
        profile.update(dtype=rasterio.float32, transform=raster1_transform)
        with rasterio.open("data/depth_barycentric_interpolation.tif", 'w', **profile) as dst:
            dst.write(difference.astype(rasterio.float32), 1)
    return