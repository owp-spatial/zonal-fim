# barrycentric.py

import warnings
import duckdb
import ibis
from ibis import _
from tqdm import tqdm
import numpy as np
import os
import shutil


def calculate_slope(vertex: np.ndarray, other_vertex1: np.ndarray, other_vertex2: np.ndarray) -> float:
    """
    The `calculate_slope` function calculates the average slope at a given vertex relative to two other vertices. 
    The slope represents the rate of change in elevation (z-coordinate) over the horizontal distance.

    Input:
        - vertex (np.ndarray): 
            A 1D NumPy array representing the coordinates [x, y, z] of the primary vertex.

        - other_vertex1 (np.ndarray): 
            A 1D NumPy array representing the coordinates [x, y, z] of the first neighboring vertex.

        - other_vertex2 (np.ndarray): 
            A 1D NumPy array representing the coordinates [x, y, z] of the second neighboring vertex.

    Output:
        - float: 
            The average slope at the given vertex, computed as the mean of the slopes 
            calculated between the primary vertex and the two neighboring vertices.

    Example:
        1. Call the function:
            slope = calculate_slope(vertex, other_vertex1, other_vertex2)

        2. Result:
            The function returns the average slope based on the two neighboring vertices.

    Notes:
        - The function assumes the input coordinates are in the same coordinate system.
        - Returns the slope as a positive value (absolute).
    """

    # Vectors from the vertex to the other two vertices
    vector1 = other_vertex1 - vertex
    vector2 = other_vertex2 - vertex
    
    # Calculate the change in elevation over the distance
    dz1 = vector1[2]  
    dz2 = vector2[2]  
    
    # Calculate the distances between the vertices in the horizontal plane 
    distance1 = np.linalg.norm(vector1[:2])  
    distance2 = np.linalg.norm(vector2[:2])  
    
    # Calculate the slope (rate of change in elevation) for each vector
    slope1 = abs(dz1 / distance1) if distance1 != 0 else 0
    slope2 = abs(dz2 / distance2) if distance2 != 0 else 0
    
    # The final slope is the average of the two slopes
    return (slope1 + slope2) / 2

def calculate_barycentric_weights(triangle_points: np.ndarray) -> np.ndarray:
    """
    The `calculate_barycentric_weights` function calculates the barycentric weights for a 3D triangle's vertices 
    based on their slopes and spatial position. These weights are used to evaluate contributions of each 
    vertex to the triangle's centroid or interpolation.

    Input:
        - triangle_points (np.ndarray): 
            A 2D NumPy array of shape (3, 3) representing the coordinates [x, y, z] of the three vertices of a triangle.

    Output:
        - np.ndarray: 
            A 1D NumPy array containing the normalized barycentric weights [w_A, w_B, w_C] for the three vertices.

    Example:
        1. Call the function:
            weights = calculate_barycentric_weights(triangle_points)

        2. Result:
            The function returns the barycentric weights as a NumPy array, e.g., [0.33, 0.33, 0.33].

    Notes:
        - Handles flat regions by assigning equal weights to all vertices.
        - Ensures weights are normalized and sum to 1.
    """
    # Calculate the slopes for each vertex
    A_point = triangle_points[0]
    B_point = triangle_points[1]
    C_point = triangle_points[2]
    slope_A = calculate_slope(A_point, B_point, C_point)
    slope_B = calculate_slope(B_point, A_point, C_point)
    slope_C = calculate_slope(C_point, A_point, B_point)

    # Normalize the slopes so that they sum to 1
    total_slope = slope_A + slope_B + slope_C
    # Set equal weights if it is a flat region
    if total_slope == 0: 
        w_A = 0.33333333333
        w_B = 0.33333333333
        w_C = 0.33333333333
    else:
        w_A = slope_A / total_slope
        w_B = slope_B / total_slope
        w_C = slope_C / total_slope

    # Compute the weighted centroid based on the slope weights
    centroid = (w_A * A_point + w_B * B_point + w_C * C_point)
    
    # Calculate vectors from each vertex to the centroid
    vec_A = centroid - A_point
    vec_B = centroid - B_point
    vec_C = centroid - C_point

    # Calculate the normal vector to the triangle 
    normal = np.cross(B_point - A_point, C_point - A_point)
    normal_length = np.linalg.norm(normal)

    # Calculate areas for weights as the magnitude of the cross products
    area_A = np.linalg.norm(np.cross(vec_B, vec_C)) / normal_length
    area_B = np.linalg.norm(np.cross(vec_C, vec_A)) / normal_length
    area_C = np.linalg.norm(np.cross(vec_A, vec_B)) / normal_length

    # Total area for normalization
    total_area = area_A + area_B + area_C

    # Calculate normalized barycentric weights
    weight_A = area_A / total_area
    weight_B = area_B / total_area
    weight_C = area_C / total_area

    weights_3d = np.array([weight_A, weight_B, weight_C])
    return weights_3d

def compute_3d_barycentric(database_path: str, node_table_name: str, element_table_name: str) -> None:
    """
    The `compute_3d_barycentric` function calculates and associates barycentric weights for 3D triangular elements
    in a geospatial database. It processes a node table and an element table to derive weights for interpolation 
    and stores the results for further use.

    Input:
        - database_path (str): 
            Path to the DuckDB database containing the node and element tables.
        - mask_database_path (str): 
            Path to a second DuckDB database containing geometry for masking purposes.
        - node_table_name (str): 
            Name of the table containing nodes with coordinates (latitude, longitude, elevation).
        - element_table_name (str): 
            Name of the table containing triangular elements defined by node references.

    Output:
        - None: 
            Results are stored in the database, including barycentric weights and associated data.

    Example:
        1. Define inputs:
            database_path = "path/to/database.duckdb"
            mask_database_path = "path/to/mask_database.duckdb"
            node_table_name = "nodes"
            element_table_name = "triangles"

        2. Call the function:
            compute_3d_barycentric(database_path, mask_database_path, node_table_name, element_table_name)

    Notes:
        - Processes triangle data in batches to handle large datasets efficiently.
        - Handles CRS metadata and geometry restoration for further geospatial analysis.
    """
    data_conn = ibis.duckdb.connect(database_path)
    try:
        data_conn.raw_sql('LOAD spatial')
    except: 
        data_conn.raw_sql('INSTALL spatial')
        data_conn.raw_sql('LOAD spatial')
    nodes_df = data_conn.table(node_table_name).execute()
    node_coords_dict = nodes_df.set_index('node_id')[['long', 'lat', 'elevation']].to_dict('index')
    triangles_df = data_conn.table(element_table_name).execute()

    # Process triangles in batches
    w_A_list = []
    w_B_list = []
    w_C_list = []

    for index, row in tqdm(triangles_df.iterrows(), total=len(triangles_df), desc="Processing Triangles"):
        node_ids = [row['node_id_1'], row['node_id_2'], row['node_id_3']]
        
        # Fetch coordinates for the current triangle's nodes
        node_coords = [node_coords_dict.get(node_id) for node_id in node_ids]
        if all(coord is not None for coord in node_coords):
            triangle_points_dem = np.array([list(coord.values()) for coord in node_coords])
        else:
            print (f"problem fetching triangle points with id {row['pg_id']} and node_ids {node_ids}")
            w_A_list.append(None)
            w_B_list.append(None)
            w_C_list.append(None)
            continue
        
        # Calculate barycentric weights
        weights = calculate_barycentric_weights(triangle_points_dem)
        
        # Append weights to the lists
        w_A_list.append(weights[0])
        w_B_list.append(weights[1])
        w_C_list.append(weights[2])

    # add the list of values as new columns in the table
    triangles_df['node1_weight'] = w_A_list
    triangles_df['node2_weight'] = w_B_list
    triangles_df['node3_weight'] = w_C_list

    output_folder = 'temp'  
    os.makedirs(output_folder, exist_ok=True)
    
    # Merge with triangles
    output_path = os.path.join(output_folder, 'bary_weights.parquet')
    triangles_df.to_parquet(output_path, index=False)
    data_conn.raw_sql(
        f"""
        CREATE OR REPLACE TABLE triangle_weights AS
        SELECT * FROM '{output_path}';
        """
    )
    
    # Save crs info
    data_conn.raw_sql(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            table_name STRING,
            crs STRING
        );
        """
    )
    data_conn.raw_sql(
        """
        INSERT INTO metadata (table_name, crs)
        VALUES 
            ('triangle_weights', 'EPSG:4326');             
        """
    )

    # Look for any problems
    triangle_elements = data_conn.table('triangle_weights')
    nan_counts = triangle_elements.aggregate(
        **{
            column: triangle_elements[column].isnull().sum().name(f"{column}_nan_count")
            for column in triangle_elements.columns
        }
    )
    nan_counts_result = nan_counts.execute()
    nan_flag = nan_counts_result.sum().sum()
    if nan_flag != 0:
        print("Found nan in triangles check previous steps")
        print(nan_counts_result)

    # Clean up
    if os.path.exists(output_folder):
        shutil.rmtree(output_folder)
    else:
        print(f"The folder '{output_folder}' does not exist.")
    data_conn.con.close()
    return
