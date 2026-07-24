import numpy as np
import pygame
import random
import sys
import math
import os
from functools import lru_cache
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from weather_system import WeatherSystem, KIND_RAIN, KIND_SNOW, KIND_SAND
from sun_system import SunSystem
from lighting_system import LightingSystem
from sound_system import (SoundSystem, BIOME_FOREST, BIOME_PLAINS, BIOME_DESERT,
                           BIOME_WATER, BIOME_MOUNTAINS, BIOME_SWAMP,
                           WEATHER_RAIN, WEATHER_SNOW, WEATHER_SAND)


class VectorizedPerlin:

    def __init__(self, seed=0):
        rng = np.random.RandomState(seed)
        perm = np.arange(256, dtype=np.int32)
        rng.shuffle(perm)
        # We duplicate the permutation table to avoid worrying about index overflow.
        self.perm = np.concatenate([perm, perm])

    @staticmethod
    def _fade(t):
        return t * t * t * (t * (t * 6 - 15) + 10)

    @staticmethod
    def _lerp(t, a, b):
        return a + t * (b - a)

    @staticmethod
    def _grad(hash_, x, y):
        h = hash_ & 3
        u = np.where(h < 2, x, y)
        v = np.where(h < 2, y, x)
        return np.where((h & 1) == 0, u, -u) + np.where((h & 2) == 0, v, -v)

    def _noise2d(self, x, y):
        xi = np.floor(x).astype(np.int64) & 255
        yi = np.floor(y).astype(np.int64) & 255
        xf = x - np.floor(x)
        yf = y - np.floor(y)

        u = self._fade(xf)
        v = self._fade(yf)

        perm = self.perm
        aa = perm[perm[xi] + yi]
        ab = perm[perm[xi] + yi + 1]
        ba = perm[perm[xi + 1] + yi]
        bb = perm[perm[xi + 1] + yi + 1]

        x1 = self._lerp(u, self._grad(aa, xf, yf), self._grad(ba, xf - 1, yf))
        x2 = self._lerp(u, self._grad(ab, xf, yf - 1), self._grad(bb, xf - 1, yf - 1))

        return self._lerp(v, x1, x2)

    def __call__(self, x, y, octaves=4, persistence=0.5, lacunarity=2.0):
        total = np.zeros_like(x, dtype=np.float64)
        amplitude = 1.0
        frequency = 1.0
        max_value = 0.0
        for _ in range(octaves):
            total += self._noise2d(x * frequency, y * frequency) * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_value

class ChunkLoader:
    def __init__(self, world, num_workers=None):
        if num_workers is None:
            cpu_count = os.cpu_count() or 1
            num_workers = max(1, min(4, cpu_count - 1)) if cpu_count > 1 else 1
        self.world = world
        self.load_queue = deque()
        self.loading = {}
        self.loaded = {}
        self.lock = threading.Lock()
        self.running = True
        
        self.executor = ThreadPoolExecutor(max_workers=num_workers)
        self.dispatcher_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self.dispatcher_thread.start()
    
    def request_load(self, chunk_x, chunk_y):
        key = f"{chunk_x},{chunk_y}"
        with self.lock:
            if key not in self.loaded and key not in self.loading and key not in self.load_queue:
                self.load_queue.append((chunk_x, chunk_y))
    
    def get_chunk(self, chunk_x, chunk_y):
        key = f"{chunk_x},{chunk_y}"
        with self.lock:
            return self.loaded.get(key)
    
    def is_loaded(self, chunk_x, chunk_y):
        key = f"{chunk_x},{chunk_y}"
        with self.lock:
            return key in self.loaded
    
    def _dispatch_loop(self):
        while self.running:
            if self.load_queue:
                with self.lock:
                    chunk_x, chunk_y = self.load_queue.popleft()
                    key = f"{chunk_x},{chunk_y}"
                    self.loading[key] = True
                self.executor.submit(self._generate_and_store, chunk_x, chunk_y, key)
            else:
                time.sleep(0.005)
    
    def _generate_and_store(self, chunk_x, chunk_y, key):
        chunk = self.world.generate_chunk_data(chunk_x, chunk_y)
        with self.lock:
            self.loading.pop(key, None)
            if chunk:
                self.loaded[key] = chunk
    
    def stop(self):
        self.running = False
        self.dispatcher_thread.join(timeout=1.0)
        self.executor.shutdown(wait=False, cancel_futures=True)


class IsometricWorld:
    def __init__(self, width=128, height=128, chunk_size=16, tile_size=16):
        self.width = width
        self.height = height
        self.chunk_size = chunk_size
        self.tile_size = tile_size
        self.num_chunks_x = width // chunk_size
        self.num_chunks_y = height // chunk_size
        self.max_layers = 16
        
        # Perlin noise settings for natural generation
        self.seed = random.randint(0, 10000)
        
        # Multiple layers of noise for different scales.
        self.noise_large = VectorizedPerlin(seed=self.seed)
        self.noise_medium = VectorizedPerlin(seed=self.seed + 1)
        self.noise_small = VectorizedPerlin(seed=self.seed + 2)
        self.noise_micro = VectorizedPerlin(seed=self.seed + 3)
        self.noise_caves = VectorizedPerlin(seed=self.seed + 4)
        self.noise_cave_detail = VectorizedPerlin(seed=self.seed + 5)
        
        # Additional noise for surface detail enhancement
        self.noise_ridge = VectorizedPerlin(seed=self.seed + 6)
        self.noise_forest = VectorizedPerlin(seed=self.seed + 7)
        
        self._compute_global_height_range()
        
        # Chunk loader
        self.loader = ChunkLoader(self)
        
        # Tile types (surface) – Layer 0
        self.tile_types = {
            'deep_ocean': 0,
            'ocean': 1,
            'shallow_water': 2,
            'beach': 3,
            'grassland': 4,
            'forest': 5,
            'dense_forest': 6,
            'hills': 7,
            'mountains': 8,
            'high_peaks': 9,
            'desert': 10,
            'savanna': 11,
            'taiga': 12,
            'tundra': 13,
            'swamp': 14,
            'snow': 15
        }
        
        # Tile types (caves) for layers 1–15 (not used)
        self.cave_tile_types = {
            'empty': 0,
            'cave_wall': 1,
            'cave_floor': 2,
            'deep_cave': 3,
            'mushroom_forest': 4,
            'underground_lake': 5,
            'lava_pocket': 6,
            'ore_vein': 7,
            'dungeon': 8,
            'crystal_cave': 9,
            'fossil_layer': 10,
            'magma_core': 11
        }
        
        # Correspondence of surface types to cave types for layers 1–3 (not used)
        self.surface_to_cave = {
            0: 5,  # deep_ocean -> underground_lake
            1: 5,  # ocean -> underground_lake
            2: 5,  # shallow_water -> underground_lake
            3: 2,  # beach -> cave_floor
            4: 2,  # grassland -> cave_floor
            5: 2,  # forest -> cave_floor
            6: 2,  # dense_forest -> cave_floor
            7: 2,  # hills -> cave_floor
            8: 1,  # mountains -> cave_wall
            9: 1,  # high_peaks -> cave_wall
            10: 2, # desert -> cave_floor
            11: 2, # savanna -> cave_floor
            12: 2, # taiga -> cave_floor
            13: 2, # tundra -> cave_floor
            14: 5, # swamp -> underground_lake
            15: 1  # snow -> cave_wall
        }
        
        # Surface
        self.colors = np.array([
            (0, 0, 80),      # 0: deep_ocean
            (0, 80, 160),    # 1: ocean
            (64, 164, 223),  # 2: shallow_water
            (238, 214, 175), # 3: beach
            (124, 252, 0),   # 4: grassland
            (34, 139, 34),   # 5: forest
            (0, 100, 0),     # 6: dense_forest
            (139, 137, 112), # 7: hills
            (128, 128, 128), # 8: mountains
            (205, 205, 205), # 9: high_peaks
            (238, 203, 173), # 10: desert
            (222, 184, 135), # 11: savanna
            (85, 107, 47),   # 12: taiga
            (200, 220, 255), # 13: tundra
            (79, 99, 86),    # 14: swamp
            (255, 255, 255)  # 15: snow
        ], dtype=np.uint8)
        
        # Cave (not used)
        self.cave_colors = np.array([
            (10, 10, 15),     # 0: empty
            (60, 45, 30),     # 1: cave_wall
            (45, 35, 25),     # 2: cave_floor
            (25, 20, 15),     # 3: deep_cave
            (80, 50, 80),     # 4: mushroom_forest
            (30, 40, 60),     # 5: underground_lake
            (100, 40, 20),    # 6: lava_pocket
            (70, 60, 40),     # 7: ore_vein
            (50, 40, 50),     # 8: dungeon
            (90, 70, 100),    # 9: crystal_cave
            (65, 55, 40),     # 10: fossil_layer
            (80, 30, 20)      # 11: magma_core
        ], dtype=np.uint8)
        
        # Tile color cache
        self.color_cache = {}
        self.cave_color_cache = {}
        
        # Central chunk
        self.center_chunk_x = self.num_chunks_x // 2
        self.center_chunk_y = self.num_chunks_y // 2
        
        print(f"generation world {width}x{height} with chunks {chunk_size}x{chunk_size}")
        print(f"Total chunks: {self.num_chunks_x * self.num_chunks_y}")
        print(f"Seed: {self.seed}")
        print(f"central chunk: ({self.center_chunk_x}, {self.center_chunk_y})")
        
        # Load the central chunk synchronously.
        self.loader.request_load(self.center_chunk_x, self.center_chunk_y)
        
        # Waiting for the central chunk to load.
        for _ in range(100):
            if self.loader.is_loaded(self.center_chunk_x, self.center_chunk_y):
                print("The central chunk has been loaded!")
                break
            time.sleep(0.01)
    
    def _raw_height(self, nx, ny):
        distance_from_center = np.sqrt(nx*nx + ny*ny) * 1.5
        
        large_scale = self.noise_large(nx * 0.3, ny * 0.3, octaves=3) * 1.5
        medium_scale = self.noise_medium(nx * 1.0, ny * 1.0, octaves=4) * 0.8
        small_scale = self.noise_small(nx * 2.0, ny * 2.0, octaves=5) * 0.4
        micro_scale = self.noise_micro(nx * 4.0, ny * 4.0, octaves=6) * 0.15
        
        continental_factor = np.maximum(0, 1 - distance_from_center * 0.9)
        
        height = (large_scale * 0.7 + medium_scale * 0.35 + small_scale * 0.15 + micro_scale * 0.08)
        height = height * continental_factor
        
        mountain_noise = self.noise_medium(nx * 2.5, ny * 2.5, octaves=4)
        mountain_chain = np.abs(mountain_noise) * 1.0
        height += mountain_chain * (height + 0.3) * 0.5
        
        return height
    
    def _compute_global_height_range(self):
        sample_res = 200
        xs = np.linspace(-0.5, 0.5, sample_res)
        ys = np.linspace(-0.5, 0.5, sample_res)
        nx, ny = np.meshgrid(xs, ys, indexing='ij')
        raw_height = self._raw_height(nx, ny)
        self.height_min = float(np.min(raw_height))
        self.height_max = float(np.max(raw_height))
    
    def generate_chunk_data(self, chunk_x, chunk_y):
        size = self.chunk_size
        x = np.arange(size)
        y = np.arange(size)
        xx, yy = np.meshgrid(x, y, indexing='ij')
        
        # World coordinates
        world_x = xx + chunk_x * size
        world_y = yy + chunk_y * size
        nx = world_x / self.width - 0.5
        ny = world_y / self.height - 0.5
        
        # Height generation (raw, without normalization within the chunk)
        height = self._raw_height(nx, ny)
        

        height = (height - self.height_min) / (self.height_max - self.height_min + 0.001) * 2.5 - 1.25
        height = np.clip(height, -1.2, 1.2)
        
        # Humidity
        base_moisture = self.noise_medium(nx * 2.0 + 100, ny * 2.0 + 100, octaves=4)
        height_factor = np.maximum(0, 1 - (height + 0.5) * 0.8)
        rain_shadow = np.where((world_x > self.width // 2) & (height > 0.2), 0.5, 1.0)
        moisture = np.clip(base_moisture * height_factor * rain_shadow, -1.0, 1.0)
        
        # Tempreture
        latitude_factor = 1 - np.abs(ny) * 1.8
        temp_drop = np.maximum(0, height * 0.5)
        base_temp = self.noise_medium(nx * 1.5 + 200, ny * 1.5 + 200, octaves=4)
        temperature = np.clip((base_temp * 0.3 + latitude_factor * 0.7) - temp_drop, -1.0, 1.0)
        
        # Fertility
        moisture_norm = (moisture + 1) / 2
        temp_norm = (temperature + 1) / 2
        fertility = np.clip(1 - np.abs(moisture_norm - 0.5) - np.abs(temp_norm - 0.5), 0, 1)
        
        # Tile map generation
        tile_map = self.generate_tile_map(height, moisture, temperature, fertility)
        
        # Application of detailing
        detail_height = self.apply_terrain_details_in_chunk(tile_map, height, chunk_x, chunk_y)
        
        # Creating a chunk
        chunk = {
            'chunk_x': chunk_x,
            'chunk_y': chunk_y,
            'is_loaded': True,
            'height_map': height,
            'moisture_map': moisture,
            'temperature_map': temperature,
            'fertility_map': fertility,
            'detail_height_map': detail_height,
            'tile_map': tile_map,
            'cave_maps': {},
            'cave_density_maps': {},
            'biome_maps': {},
            'cave_tile_maps': {}
        }
        
        # We utilize forest heights.
        self.apply_forest_heights_in_chunk(chunk)
        
        
        return chunk
    
    def generate_tile_map(self, height, moisture, temp, fertility):
        tile_map = np.zeros((self.chunk_size, self.chunk_size), dtype=int)
        
        tile_map[height < -0.8] = self.tile_types['deep_ocean']
        tile_map[(height >= -0.8) & (height < -0.4)] = self.tile_types['ocean']
        tile_map[(height >= -0.4) & (height < -0.2)] = self.tile_types['shallow_water']
        tile_map[(height >= -0.2) & (height < -0.05)] = self.tile_types['beach']
        
        land = height >= -0.05
        
        cold = (temp < -0.3) & land
        tile_map[cold & (moisture > 0)] = self.tile_types['taiga']
        tile_map[cold & (moisture <= 0)] = self.tile_types['tundra']
        
        hot = (temp > 0.3) & land
        tile_map[hot & (moisture < -0.2)] = self.tile_types['desert']
        tile_map[hot & (moisture >= -0.2) & (moisture < 0.2)] = self.tile_types['savanna']
        tile_map[hot & (moisture >= 0.2)] = self.tile_types['grassland']
        
        temperate = (~cold) & (~hot) & land
        tile_map[temperate & (moisture < -0.1)] = self.tile_types['grassland']
        
        forest_cond = temperate & (moisture >= -0.1) & (moisture < 0.3)
        forest_height_cond = height > 0.08
        dense_forest_cond = (height > 0.15) & (fertility > 0.6)
        
        tile_map[forest_cond & forest_height_cond & (fertility > 0.7)] = self.tile_types['dense_forest']
        tile_map[forest_cond & forest_height_cond & (fertility <= 0.7)] = self.tile_types['forest']
        
        tile_map[temperate & (moisture >= 0.3) & (height < 0.1)] = self.tile_types['swamp']
        
        hills_cond = (height > 0.35) & (height <= 0.6) & land
        mountains_cond = (height > 0.6) & (height <= 0.9) & land
        peaks_cond = (height > 0.9) & land
        
        tile_map[hills_cond] = self.tile_types['hills']
        tile_map[mountains_cond] = self.tile_types['mountains']
        tile_map[peaks_cond] = self.tile_types['high_peaks']
        tile_map[height > 1.0] = self.tile_types['snow']
        
        return tile_map
    
    def apply_terrain_details_in_chunk(self, tile_map, height_map, chunk_x, chunk_y):
        size = self.chunk_size
        detail_height = height_map.copy()
        
        x = np.arange(size)
        y = np.arange(size)
        xx, yy = np.meshgrid(x, y, indexing='ij')
        
        world_x = xx + chunk_x * size
        world_y = yy + chunk_y * size
        nx = world_x / self.width - 0.5
        ny = world_y / self.height - 0.5
        
        hill_noise = self.noise_small(nx * 3.0, ny * 3.0, octaves=5) * 0.25
        ridge_noise = self.noise_ridge(nx * 2.0, ny * 2.0, octaves=5) * 0.35
        forest_noise = self.noise_forest(nx * 1.5, ny * 1.5, octaves=4) * 0.18
        dune_noise = self.noise_small(nx * 4.0, ny * 4.0, octaves=5)
        
        mountain_mask = np.isin(tile_map, [self.tile_types['mountains'],
                                            self.tile_types['high_peaks'],
                                            self.tile_types['snow']])
        hills_mask = tile_map == self.tile_types['hills']
        forest_mask = np.isin(tile_map, [self.tile_types['forest'],
                                          self.tile_types['dense_forest'],
                                          self.tile_types['taiga']])
        grass_mask = np.isin(tile_map, [self.tile_types['grassland'], self.tile_types['savanna']])
        desert_mask = tile_map == self.tile_types['desert']
        
        detail_height[mountain_mask] += np.abs(ridge_noise[mountain_mask]) * 0.5
        detail_height[mountain_mask] += hill_noise[mountain_mask] * 0.8
        
        detail_height[hills_mask] += hill_noise[hills_mask] * 1.2
        detail_height[hills_mask] += ridge_noise[hills_mask] * 0.35
        
        detail_height[forest_mask] += forest_noise[forest_mask] * 0.5
        forest_bonus_mask = forest_mask & (forest_noise > 0.2)
        detail_height[forest_bonus_mask] += hill_noise[forest_bonus_mask] * 0.5
        
        detail_height[grass_mask] += hill_noise[grass_mask] * 0.25
        
        detail_height[desert_mask] += dune_noise[desert_mask] * 0.15
        
        detail_height = np.clip(detail_height, -1.2, 1.2)
        
        return detail_height
    
    def apply_forest_heights_in_chunk(self, chunk):
        size = self.chunk_size
        x = np.arange(size)
        y = np.arange(size)
        xx, yy = np.meshgrid(x, y, indexing='ij')
        
        world_x = xx + chunk['chunk_x'] * size
        world_y = yy + chunk['chunk_y'] * size
        nx = world_x / self.width - 0.5
        ny = world_y / self.height - 0.5
        
        forest_terrain = self.noise_forest(nx * 2.0, ny * 2.0, octaves=4)
        
        # Vectorized using NumPy masks instead of a nested Python loop.
        tile_map = chunk['tile_map']
        forest_mask = np.isin(tile_map, [self.tile_types['forest'], self.tile_types['dense_forest']])
        taiga_mask = tile_map == self.tile_types['taiga']
        
        chunk['height_map'][forest_mask] += (forest_terrain[forest_mask] + 0.3) * 0.25
        forest_bonus_mask = forest_mask & (forest_terrain > 0.3)
        chunk['height_map'][forest_bonus_mask] += 0.08
        
        chunk['height_map'][taiga_mask] += (forest_terrain[taiga_mask] + 0.1) * 0.18
        
        chunk['height_map'] = np.clip(chunk['height_map'], -1.2, 1.2)
    
    def get_chunk(self, chunk_x, chunk_y):
        if chunk_x < 0 or chunk_x >= self.num_chunks_x or chunk_y < 0 or chunk_y >= self.num_chunks_y:
            return None
        
        self.loader.request_load(chunk_x, chunk_y)
        return self.loader.get_chunk(chunk_x, chunk_y)
    
    def get_tile_color(self, chunk, x, y, layer=0):
        if chunk is None:
            return (40, 40, 50)
        
        if layer == 0:
            tile_type = chunk['tile_map'][x, y]
            base_color = self.colors[tile_type]
            
            height = chunk['height_map'][x, y]
            moisture = chunk['moisture_map'][x, y]
            
            brightness = 0.8 + height * 0.4
            brightness = max(0.6, min(1.0, brightness))
            
            if tile_type in [self.tile_types['mountains'], self.tile_types['high_peaks'], self.tile_types['snow']]:
                brightness = min(1.0, brightness * 1.15)
                if x > 0 and y > 0:
                    slope_effect = abs(chunk['height_map'][x, y] - chunk['height_map'][x-1, y]) * 0.4
                    brightness += slope_effect
            
            if tile_type == self.tile_types['hills']:
                hill_brightness = (height - 0.35) * 0.5
                brightness += max(0, hill_brightness)
            
            if tile_type in [self.tile_types['forest'], self.tile_types['dense_forest']]:
                forest_height = max(0, (height - 0.0) * 0.35)
                brightness += forest_height * 0.25
                
                green_factor = (moisture + 1) * 0.3 + (height * 0.25)
                r = int(base_color[0] * brightness * (1 - green_factor * 0.3))
                g = int(base_color[1] * brightness * (1 + green_factor * 0.4))
                b = int(base_color[2] * brightness * (1 - green_factor * 0.2))
                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))
                return (r, g, b)
            
            if tile_type == self.tile_types['taiga']:
                forest_height = max(0, (height - 0.0) * 0.25)
                brightness += forest_height * 0.2
                r = int(base_color[0] * brightness)
                g = int(base_color[1] * brightness * 1.1)
                b = int(base_color[2] * brightness * 0.9)
                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))
                return (r, g, b)
            
            r = max(0, min(255, int(base_color[0] * brightness)))
            g = max(0, min(255, int(base_color[1] * brightness)))
            b = max(0, min(255, int(base_color[2] * brightness)))
            return (r, g, b)
        else:
            if layer not in chunk['cave_tile_maps']:
                return (40, 40, 50)
            cave_type = chunk['cave_tile_maps'][layer][x, y]
            base_color = self.cave_colors[cave_type]
            depth = layer / (self.max_layers - 1)
            brightness = 0.4 + depth * 0.3
            brightness = max(0.3, min(0.8, brightness))
            
            r = max(0, min(255, int(base_color[0] * brightness)))
            g = max(0, min(255, int(base_color[1] * brightness)))
            b = max(0, min(255, int(base_color[2] * brightness)))
            return (r, g, b)
    
    def get_tile(self, world_x, world_y, layer=0):
        if world_x < 0 or world_x >= self.width or world_y < 0 or world_y >= self.height:
            return (0, 0, 0)
        
        chunk_x = world_x // self.chunk_size
        chunk_y = world_y // self.chunk_size
        local_x = world_x % self.chunk_size
        local_y = world_y % self.chunk_size
        
        chunk = self.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return (40, 40, 50)
        return self.get_tile_color(chunk, local_x, local_y, layer)
    
    def get_tile_type(self, world_x, world_y):
        if world_x < 0 or world_x >= self.width or world_y < 0 or world_y >= self.height:
            return 0
        
        chunk_x = world_x // self.chunk_size
        chunk_y = world_y // self.chunk_size
        local_x = world_x % self.chunk_size
        local_y = world_y % self.chunk_size
        
        chunk = self.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return 0
        return chunk['tile_map'][local_x, local_y]
    
    def get_height(self, world_x, world_y):
        if world_x < 0 or world_x >= self.width or world_y < 0 or world_y >= self.height:
            return 0
        
        chunk_x = world_x // self.chunk_size
        chunk_y = world_y // self.chunk_size
        local_x = world_x % self.chunk_size
        local_y = world_y % self.chunk_size
        
        chunk = self.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return 0
        return chunk['height_map'][local_x, local_y]
    
    def cartesian_to_isometric(self, x, y):
        half_tile = self.tile_size // 2
        iso_x = (x - y) * half_tile
        iso_y = (x + y) * (half_tile // 2)
        return iso_x, iso_y
    
    def isometric_to_cartesian(self, iso_x, iso_y):
        half_tile = self.tile_size // 2
        half_height = half_tile // 2
        x = (iso_x / half_tile + iso_y / half_height) / 2
        y = (iso_y / half_height - iso_x / half_tile) / 2
        return x, y


class DualViewRenderer:
    RAIN_AREA_SCALE = 15
    
    def __init__(self, world):
        self.world = world
        
        pygame.init()
        
        display_info = pygame.display.Info()
        self.screen_width = display_info.current_w
        self.screen_height = display_info.current_h
        
        self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.FULLSCREEN | pygame.HWSURFACE | pygame.DOUBLEBUF)
        pygame.display.set_caption(f"World {world.width}x{world.height} [V - вид]")
        
        self.font = pygame.font.Font(None, 28)
        self.small_font = pygame.font.Font(None, 22)
        self.big_font = pygame.font.Font(None, 48)
        
        self.isometric_mode = True
        self.current_layer = 0
        
        # A Camera at the Center of the World
        self.world_camera_x = world.width * world.tile_size // 2
        self.world_camera_y = world.height * world.tile_size // 2
        self.target_world_camera_x = self.world_camera_x
        self.target_world_camera_y = self.world_camera_y
        
        # Current camera chunk
        self.current_chunk_x = int(self.world_camera_x / world.tile_size) // world.chunk_size
        self.current_chunk_y = int(self.world_camera_y / world.tile_size) // world.chunk_size
        
        # For a smooth transition
        self.is_transitioning = False
        self.chunk_transition = 1.0
        self.old_chunk_data = None
        
        self.isometric_zoom = 2.5
        self.topdown_zoom = 2
        self.target_isometric_zoom = 2.5
        self.current_zoom = self.isometric_zoom
        
        self.show_grid = False

        self.selected_tile = None
        self.show_info = True
        self.show_minimap = True
        
        self.clock = pygame.time.Clock()
        self.dt = 1.0
        
        # The weather mechanic (rain) has been moved to a separate module, `weather_system.py`.
        self.weather = WeatherSystem(self.screen_width, self.screen_height, seed=world.seed)
        
        # Sun mechanics (day/night cycle)
        self.sun = SunSystem(self.screen_width, self.screen_height, start_time=0.45)
        
        # The manual lighting mechanic (flashlights) is a separate module: `lighting_system.py`.
        self.lighting = LightingSystem()
        
        # Sound is a separate module, sound_system.py: biome ambient sound
        self.sound = SoundSystem()
        self._sound_biome_category = None
        
        self.visible_tiles_count = 0
        self.total_tiles = world.width * world.height
        self.fps_history = []
        
        self.water_phase = 0
        self.lava_phase = 0
        
        print("Preliminary calculation of tile coordinates...")
        self.iso_coords = {}
        # We pre-cache the coordinates of the central chunk.
        center_chunk_x = world.center_chunk_x
        center_chunk_y = world.center_chunk_y
        self.get_chunk_iso_coords(center_chunk_x, center_chunk_y)
        print("Complete!")
        
        self.iso_surface = None
        self.last_zoom = 0
        self.last_camera_x = 0
        self.last_camera_y = 0
        
        self.color_surfaces = {}
        
        self.target_world_camera_x = self.world_camera_x
        self.target_world_camera_y = self.world_camera_y
        
        self.minimap_size = 200
        self.minimap_x = self.screen_width - self.minimap_size - 20
        self.minimap_y = 20
        
        # Preloading adjacent chunks
        self.preload_radius = 2
        self.preload_chunks()
    
    def preload_chunks(self):
        for dx in range(-self.preload_radius, self.preload_radius + 1):
            for dy in range(-self.preload_radius, self.preload_radius + 1):
                cx = self.current_chunk_x + dx
                cy = self.current_chunk_y + dy
                if 0 <= cx < self.world.num_chunks_x and 0 <= cy < self.world.num_chunks_y:
                    self.world.get_chunk(cx, cy)
    
    def update_chunk(self):
        new_chunk_x = int(self.world_camera_x / self.world.tile_size) // self.world.chunk_size
        new_chunk_y = int(self.world_camera_y / self.world.tile_size) // self.world.chunk_size
        
        if new_chunk_x < 0 or new_chunk_x >= self.world.num_chunks_x:
            new_chunk_x = self.current_chunk_x
        if new_chunk_y < 0 or new_chunk_y >= self.world.num_chunks_y:
            new_chunk_y = self.current_chunk_y
        
        if new_chunk_x != self.current_chunk_x or new_chunk_y != self.current_chunk_y:
            self.is_transitioning = True
            self.chunk_transition = 0.0
            
            self.old_chunk_data = self.world.get_chunk(self.current_chunk_x, self.current_chunk_y)
            
            self.current_chunk_x = new_chunk_x
            self.current_chunk_y = new_chunk_y
            
            self.preload_chunks()
            print(f"Jump to chunk: ({self.current_chunk_x}, {self.current_chunk_y})")
    
    def get_chunk_iso_coords(self, chunk_x, chunk_y):
        key = f"{chunk_x},{chunk_y}"
        if key not in self.iso_coords:
            size = self.world.chunk_size
            coords = np.zeros((size, size, 2))
            for x in range(size):
                for y in range(size):
                    world_x = chunk_x * size + x
                    world_y = chunk_y * size + y
                    coords[x, y] = self.world.cartesian_to_isometric(world_x, world_y)
            self.iso_coords[key] = coords
        return self.iso_coords[key]
    
    def handle_input(self):
        keys = pygame.key.get_pressed()
        dt = self.dt
        
        if self.isometric_mode:
            speed = 15 * dt / self.current_zoom
        else:
            speed = 15 * dt / self.topdown_zoom
        
        if keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]:
            speed *= 3
        
        move_x = move_y = 0
        
        if keys[pygame.K_LEFT] or keys[pygame.K_a]:
            move_x = -speed
        if keys[pygame.K_RIGHT] or keys[pygame.K_d]:
            move_x = speed
        if keys[pygame.K_UP] or keys[pygame.K_w]:
            move_y = -speed
        if keys[pygame.K_DOWN] or keys[pygame.K_s]:
            move_y = speed
        
        self.target_world_camera_x += move_x
        self.target_world_camera_y += move_y
        
        max_x = (self.world.width - 1) * self.world.tile_size
        max_y = (self.world.height - 1) * self.world.tile_size
        self.target_world_camera_x = max(0, min(max_x, self.target_world_camera_x))
        self.target_world_camera_y = max(0, min(max_y, self.target_world_camera_y))
        
        self.world_camera_x += (self.target_world_camera_x - self.world_camera_x) * 0.2
        self.world_camera_y += (self.target_world_camera_y - self.world_camera_y) * 0.2
        
        # Updating chunk
        self.update_chunk()
        
        # Updating the transition
        if self.is_transitioning:
            self.chunk_transition += dt * 0.08
            if self.chunk_transition >= 1.0:
                self.chunk_transition = 1.0
                self.is_transitioning = False
                self.old_chunk_data = None
        
        if self.isometric_mode:
            self.isometric_zoom += (self.target_isometric_zoom - self.isometric_zoom) * 0.15
            self.current_zoom = self.isometric_zoom
        else:
            self.current_zoom = self.topdown_zoom
    
    def handle_minimap_click(self, mouse_x, mouse_y):
        if (self.minimap_x <= mouse_x <= self.minimap_x + self.minimap_size and
            self.minimap_y <= mouse_y <= self.minimap_y + self.minimap_size):
            
            rel_x = mouse_x - self.minimap_x
            rel_y = mouse_y - self.minimap_y
            
            tile_x = (rel_x / self.minimap_size) * self.world.width
            tile_y = (rel_y / self.minimap_size) * self.world.height
            
            tile_x = max(0, min(self.world.width - 1, tile_x))
            tile_y = max(0, min(self.world.height - 1, tile_y))
            
            self.target_world_camera_x = tile_x * self.world.tile_size
            self.target_world_camera_y = tile_y * self.world.tile_size
            
            print(f"Moving the camera to a tile ({int(tile_x)}, {int(tile_y)})")
            return True
        return False
    
    def get_isometric_camera_from_world(self):
        center_tile_x = self.world_camera_x / self.world.tile_size
        center_tile_y = self.world_camera_y / self.world.tile_size
        return self.world.cartesian_to_isometric(center_tile_x, center_tile_y)
    
    def get_visible_tiles_isometric(self):
        visible_tiles = []
        
        iso_camera_x, iso_camera_y = self.get_isometric_camera_from_world()
        
        half_tile = self.world.tile_size * self.current_zoom // 2
        quarter_tile = half_tile // 2
        
        # Current chunk
        chunk = self.world.get_chunk(self.current_chunk_x, self.current_chunk_y)
        if chunk is None:
            return visible_tiles
        
        iso_coords = self.get_chunk_iso_coords(self.current_chunk_x, self.current_chunk_y)
        
        for y in range(self.world.chunk_size):
            for x in range(self.world.chunk_size):
                iso_x, iso_y = iso_coords[x, y]
                
                screen_x = (iso_x - iso_camera_x) * self.current_zoom + self.screen_width // 2
                screen_y = (iso_y - iso_camera_y) * self.current_zoom + self.screen_height // 2
                
                if (screen_x + half_tile > 0 and screen_x - half_tile < self.screen_width and
                    screen_y + quarter_tile > 0 and screen_y - quarter_tile < self.screen_height):
                    visible_tiles.append((x + y, x, y, screen_x, screen_y, 1.0,
                                           self.current_chunk_x, self.current_chunk_y))
        
        # We add tiles from the old chunk for a smooth transition.
        if self.is_transitioning and self.old_chunk_data is not None:
            alpha = 1.0 - self.chunk_transition
            old_chunk_x = self.old_chunk_data['chunk_x']
            old_chunk_y = self.old_chunk_data['chunk_y']
            old_iso_coords = self.get_chunk_iso_coords(old_chunk_x, old_chunk_y)
            
            for y in range(self.world.chunk_size):
                for x in range(self.world.chunk_size):
                    iso_x, iso_y = old_iso_coords[x, y]
                    
                    screen_x = (iso_x - iso_camera_x) * self.current_zoom + self.screen_width // 2
                    screen_y = (iso_y - iso_camera_y) * self.current_zoom + self.screen_height // 2
                    
                    if (screen_x + half_tile > 0 and screen_x - half_tile < self.screen_width and
                        screen_y + quarter_tile > 0 and screen_y - quarter_tile < self.screen_height):
                        visible_tiles.append((x + y + 10000, x, y, screen_x, screen_y, alpha,
                                               old_chunk_x, old_chunk_y))
        
        visible_tiles.sort(key=lambda t: t[0])
        return visible_tiles
    
    def get_visible_tiles_topdown(self):
        visible_tiles = []
        
        tile_size_px = self.world.tile_size * self.topdown_zoom
        
        chunk = self.world.get_chunk(self.current_chunk_x, self.current_chunk_y)
        if chunk is None:
            return visible_tiles
        
        for y in range(self.world.chunk_size):
            for x in range(self.world.chunk_size):
                world_x = self.current_chunk_x * self.world.chunk_size + x
                world_y = self.current_chunk_y * self.world.chunk_size + y
                
                if world_x >= self.world.width or world_y >= self.world.height:
                    continue
                
                screen_x = (world_x * self.world.tile_size - self.world_camera_x) * self.topdown_zoom + self.screen_width // 2
                screen_y = (world_y * self.world.tile_size - self.world_camera_y) * self.topdown_zoom + self.screen_height // 2
                
                if (screen_x + tile_size_px > 0 and screen_x < self.screen_width and
                    screen_y + tile_size_px > 0 and screen_y < self.screen_height):
                    visible_tiles.append((world_x, world_y, screen_x, screen_y))
        
        if self.current_layer == 0:
            visible_tiles.sort(key=lambda t: -self.world.get_height(t[0], t[1]))
        
        return visible_tiles
    
    def render_tile_isometric(self, x, y, screen_x, screen_y, alpha=1.0, chunk_x=None, chunk_y=None):
        if chunk_x is None:
            chunk_x = self.current_chunk_x
        if chunk_y is None:
            chunk_y = self.current_chunk_y
        
        # For tiles from a chunk, we use world coordinates.
        world_x = chunk_x * self.world.chunk_size + x
        world_y = chunk_y * self.world.chunk_size + y
        
        color = self.world.get_tile(world_x, world_y, self.current_layer)
        
        if len(color) > 3:
            color = color[:3]
        color = tuple(max(0, min(255, int(c))) for c in color)
        
        # We apply transparency for a smooth transition.
        if alpha < 1.0:
            bg_color = (10, 10, 20)
            color = tuple(int(c * alpha + bg * (1 - alpha)) for c, bg in zip(color, bg_color))
        
        # Effects for different tile types
        if self.current_layer == 0:
            tile_type = self.world.get_tile_type(world_x, world_y)
            height = self.world.get_height(world_x, world_y)
            
            if tile_type <= 2:
                wave = math.sin((world_x + world_y) * 0.3 + self.water_phase) * 8
                if wave > 0:
                    color = tuple(max(0, min(255, c + int(wave))) for c in color)
            
            elif tile_type in [self.world.tile_types['mountains'], 
                              self.world.tile_types['high_peaks'],
                              self.world.tile_types['snow']]:
                if world_x > 0 and world_y > 0:
                    h1 = self.world.get_height(world_x, world_y)
                    h2 = self.world.get_height(world_x - 1, world_y)
                    h3 = self.world.get_height(world_x, world_y - 1)
                    dx = h1 - h2
                    dy = h1 - h3
                    
                    facing = dx * self._light_dx + dy * self._light_dy
                    shadow = max(0, -facing * 40) * self._sun_strength
                    slope_highlight = max(0, facing * 40) * self._sun_strength
                    color = tuple(max(0, min(255, c - int(shadow) + int(slope_highlight))) for c in color)
                    if height > 0.7:
                        highlight = int((height - 0.7) * 70 * self._sun_strength)
                        color = tuple(min(255, c + highlight) for c in color)
            
            elif tile_type == self.world.tile_types['hills']:
                if world_x > 0 and world_y > 0:
                    h1 = self.world.get_height(world_x, world_y)
                    h2 = self.world.get_height(world_x - 1, world_y)
                    h3 = self.world.get_height(world_x, world_y - 1)
                    dx = h1 - h2
                    dy = h1 - h3
                    facing = dx * self._light_dx + dy * self._light_dy
                    shadow = max(0, -facing * 20) * self._sun_strength
                    slope_highlight = max(0, facing * 20) * self._sun_strength
                    color = tuple(max(0, min(255, c - int(shadow) + int(slope_highlight))) for c in color)
            
            elif tile_type in [self.world.tile_types['forest'], 
                              self.world.tile_types['dense_forest'],
                              self.world.tile_types['taiga']]:
                if self.world.get_height(world_x, world_y) > 0.08:
                    canopy_height = (self.world.get_height(world_x, world_y) - 0.08) * 40
                    color = tuple(min(255, c + int(canopy_height * 0.6)) for c in color)
            

            color = self.sun.apply_tint(color)
            
            
            light_r, light_g, light_b = self.lighting.get_tile_light_boost(world_x, world_y)
            if light_r or light_g or light_b:
                color = (
                    min(255, int(color[0] + light_r)),
                    min(255, int(color[1] + light_g)),
                    min(255, int(color[2] + light_b)),
                )
        
        else:
            chunk = self.world.get_chunk(chunk_x, chunk_y)
            if chunk is not None and self.current_layer in chunk['cave_tile_maps']:
                cave_type = chunk['cave_tile_maps'][self.current_layer][x, y]
                if cave_type == self.world.cave_tile_types['underground_lake']:
                    wave = math.sin((world_x + world_y) * 0.3 + self.water_phase) * 10
                    color = tuple(max(0, min(255, c + int(wave))) for c in color)
                elif cave_type == self.world.cave_tile_types['lava_pocket']:
                    glow = math.sin(self.lava_phase + world_x * 0.5 + world_y * 0.3) * 15
                    color = tuple(max(0, min(255, c + int(glow))) for c in color)
                elif cave_type == self.world.cave_tile_types['crystal_cave']:
                    sparkle = math.sin(self.lava_phase * 2 + world_x * 0.8 + world_y * 0.8) * 10
                    color = tuple(max(0, min(255, c + int(sparkle))) for c in color)
        
        zoom = self.current_zoom
        half_tile = int(self.world.tile_size * zoom // 2)
        quarter_tile = half_tile // 2
        
        height_offset = 0
        if self.current_layer == 0:
            height_value = self.world.get_height(world_x, world_y)
            if height_value > 0:
                height_offset = -int(height_value * 8 * zoom)
        
        points = [
            (screen_x, screen_y - quarter_tile + height_offset),
            (screen_x + half_tile, screen_y + height_offset),
            (screen_x, screen_y + quarter_tile + height_offset),
            (screen_x - half_tile, screen_y + height_offset)
        ]
        
        pygame.draw.polygon(self.screen, color, points)
        
        if self.show_grid:
            grid_color = (30, 30, 30) if self.current_layer > 0 else (60, 60, 60)
            pygame.draw.polygon(self.screen, grid_color, points, 1)
            
            if self.selected_tile == (world_x, world_y):
                pygame.draw.polygon(self.screen, (255, 215, 0), points, 3)
    
    
    def render_minimap(self):
        minimap_size = 200
        minimap_x = self.screen_width - minimap_size - 20
        minimap_y = 20
        
        self.minimap_x = minimap_x
        self.minimap_y = minimap_y
        self.minimap_size = minimap_size
        
        minimap_surface = pygame.Surface((minimap_size, minimap_size), pygame.SRCALPHA)
        minimap_surface.fill((20, 20, 30, 255))
        
        scale_x = minimap_size / self.world.width
        scale_y = minimap_size / self.world.height
        

        with self.world.loader.lock:
            for key, chunk in list(self.world.loader.loaded.items()):
                if chunk['is_loaded']:
                    mid_x = self.world.chunk_size // 2
                    mid_y = self.world.chunk_size // 2
                    tile_type = chunk['tile_map'][mid_x, mid_y]
                    color = self.world.colors[tile_type]
                    
                    map_x = int((chunk['chunk_x'] * self.world.chunk_size) * scale_x)
                    map_y = int((chunk['chunk_y'] * self.world.chunk_size) * scale_y)
                    size_x = max(1, int(self.world.chunk_size * scale_x) + 1)
                    size_y = max(1, int(self.world.chunk_size * scale_y) + 1)
                    
                    if map_x < minimap_size and map_y < minimap_size:
                        pygame.draw.rect(minimap_surface, color, 
                                       (map_x, map_y, size_x, size_y))
        
        self.screen.blit(minimap_surface, (minimap_x, minimap_y))
        pygame.draw.rect(self.screen, (80, 80, 80), (minimap_x, minimap_y, minimap_size, minimap_size), 1)
        
        camera_tile_x = self.world_camera_x / self.world.tile_size
        camera_tile_y = self.world_camera_y / self.world.tile_size
        
        if self.isometric_mode:
            view_width = (self.screen_width / self.current_zoom) / self.world.tile_size * 1.5
            view_height = (self.screen_height / self.current_zoom) / self.world.tile_size * 1.5
        else:
            view_width = (self.screen_width / self.topdown_zoom) / self.world.tile_size
            view_height = (self.screen_height / self.topdown_zoom) / self.world.tile_size
        
        vis_width = view_width * scale_x
        vis_height = view_height * scale_y
        vis_x = minimap_x + (camera_tile_x - view_width/2) * scale_x
        vis_y = minimap_y + (camera_tile_y - view_height/2) * scale_y
        
        vis_x = max(minimap_x, min(vis_x, minimap_x + minimap_size - vis_width))
        vis_y = max(minimap_y, min(vis_y, minimap_y + minimap_size - vis_height))
        
        if vis_width > 0 and vis_height > 0:
            pygame.draw.rect(self.screen, (255, 255, 255), 
                           (int(vis_x), int(vis_y), int(vis_width), int(vis_height)), 1)
        
        center_x = minimap_x + camera_tile_x * scale_x
        center_y = minimap_y + camera_tile_y * scale_y
        
        center_x = max(minimap_x, min(center_x, minimap_x + minimap_size))
        center_y = max(minimap_y, min(center_y, minimap_y + minimap_size))
        
        pygame.draw.circle(self.screen, (255, 0, 0), (int(center_x), int(center_y)), 4)
        pygame.draw.circle(self.screen, (255, 255, 255), (int(center_x), int(center_y)), 2)
    
    def _compute_chunk_screen_area(self, visible_tiles):
        if not visible_tiles:
            return None
        
        if self.isometric_mode:
            half_tile = self.world.tile_size * self.current_zoom / 2
            quarter_tile = half_tile / 2
            
            top_tile = min(visible_tiles, key=lambda t: t[4])
            bottom_tile = max(visible_tiles, key=lambda t: t[4])
            left_tile = min(visible_tiles, key=lambda t: t[3])
            right_tile = max(visible_tiles, key=lambda t: t[3])
            
            top = (top_tile[3], top_tile[4] - quarter_tile)
            right = (right_tile[3] + half_tile, right_tile[4])
            bottom = (bottom_tile[3], bottom_tile[4] + quarter_tile)
            left = (left_tile[3] - half_tile, left_tile[4])
            
            # We enlarge the rhombus from its center—the area of ​​the rain is greater than the area...
            # the chunk itself, rather than right up against its boundary.
            points = [top, right, bottom, left]
            center_x = sum(p[0] for p in points) / 4
            center_y = sum(p[1] for p in points) / 4
            scale = self.RAIN_AREA_SCALE
            scaled_points = [
                (center_x + (px - center_x) * scale, center_y + (py - center_y) * scale)
                for px, py in points
            ]
            
            return ('polygon', scaled_points)
        else:
            tile_size_px = self.world.tile_size * self.topdown_zoom * self.RAIN_AREA_SCALE
            xs = [t[2] for t in visible_tiles]
            ys = [t[3] for t in visible_tiles]
            
            min_x = max(0, min(xs) - tile_size_px)
            max_x = min(self.screen_width, max(xs) + tile_size_px)
            min_y = max(0, min(ys) - tile_size_px)
            max_y = min(self.screen_height, max(ys) + tile_size_px)
            
            width = max_x - min_x
            height = max_y - min_y
            if width <= 0 or height <= 0:
                return None
            
            return ('rect', (min_x, min_y, width, height))
    
    # Biomes with snowfall (cold/high-altitude) and sandstorms
    # (desert/savanna). Everything else (grass, forest, hills, water, swamp) 
    _SNOW_BIOME_NAMES = ('snow', 'high_peaks', 'mountains', 'tundra', 'taiga')
    _SAND_BIOME_NAMES = ('desert', 'savanna')

    def _get_snow_biome_ids(self):
        if not hasattr(self, '_snow_biome_ids'):
            self._snow_biome_ids = {self.world.tile_types[n] for n in self._SNOW_BIOME_NAMES}
        return self._snow_biome_ids

    def _get_sand_biome_ids(self):
        if not hasattr(self, '_sand_biome_ids'):
            self._sand_biome_ids = {self.world.tile_types[n] for n in self._SAND_BIOME_NAMES}
        return self._sand_biome_ids

    _SOUND_BIOME_MAP = {
        'deep_ocean': BIOME_WATER, 'ocean': BIOME_WATER, 'shallow_water': BIOME_WATER, 'beach': BIOME_WATER,
        'grassland': BIOME_PLAINS, 'savanna': BIOME_PLAINS, 'hills': BIOME_PLAINS,
        'forest': BIOME_FOREST, 'dense_forest': BIOME_FOREST, 'taiga': BIOME_FOREST,
        'desert': BIOME_DESERT,
        'mountains': BIOME_MOUNTAINS, 'high_peaks': BIOME_MOUNTAINS, 'snow': BIOME_MOUNTAINS, 'tundra': BIOME_MOUNTAINS,
        'swamp': BIOME_SWAMP,
    }

    def _get_sound_biome_ids(self):
        if not hasattr(self, '_sound_biome_ids'):
            self._sound_biome_ids = {
                self.world.tile_types[name]: category
                for name, category in self._SOUND_BIOME_MAP.items()
            }
        return self._sound_biome_ids

    def _update_dominant_sound_biome(self, visible_tiles):
        if not visible_tiles:
            return getattr(self, '_dominant_sound_biome', None)
        
        id_to_category = self._get_sound_biome_ids()
        counts = {}
        is_iso = self.isometric_mode
        for tile in visible_tiles:
            if is_iso:
                depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y = tile
            else:
                tile_chunk_x, tile_chunk_y = self.current_chunk_x, self.current_chunk_y
                x, y, screen_x, screen_y = tile
            world_x = tile_chunk_x * self.world.chunk_size + x
            world_y = tile_chunk_y * self.world.chunk_size + y
            tile_type = self.world.get_tile_type(world_x, world_y)
            category = id_to_category.get(tile_type, BIOME_PLAINS)
            counts[category] = counts.get(category, 0) + 1
        
        if not counts:
            return getattr(self, '_dominant_sound_biome', None)
        
        HYSTERESIS_MARGIN = 1.15
        current = getattr(self, '_dominant_sound_biome', None)
        best_category = max(counts, key=lambda k: counts[k])
        
        if current is None or best_category == current:
            self._dominant_sound_biome = best_category
            return best_category
        
        current_count = counts.get(current, 0)
        if counts[best_category] > current_count * HYSTERESIS_MARGIN:
            self._dominant_sound_biome = best_category
            return best_category
        
        return current

    def _compute_biome_landing_points(self, visible_tiles):
        snow_ids = self._get_snow_biome_ids()
        sand_ids = self._get_sand_biome_ids()
        
        rain_points = []
        snow_points = []
        sand_points = []
        
        zoom = self.current_zoom
        is_iso = self.isometric_mode
        
        for tile in visible_tiles:
            if is_iso:
                depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y = tile
            else:
                tile_chunk_x, tile_chunk_y = self.current_chunk_x, self.current_chunk_y
                x, y, screen_x, screen_y = tile
            
            world_x = tile_chunk_x * self.world.chunk_size + x
            world_y = tile_chunk_y * self.world.chunk_size + y
            
            if is_iso:
                height_value = self.world.get_height(world_x, world_y)
                height_offset = -int(height_value * 8 * zoom) if height_value > 0 else 0
                point = (screen_x, screen_y + height_offset)
            else:
                # In the top-down view, height does not shift the tile's position on the screen. (not used)
                point = (screen_x, screen_y)
            
            tile_type = self.world.get_tile_type(world_x, world_y)
            if tile_type in snow_ids:
                snow_points.append(point)
            elif tile_type in sand_ids:
                sand_points.append(point)
            else:
                rain_points.append(point)
        
        return rain_points, snow_points, sand_points
    
    def _update_dominant_weather_kind(self, rain_points, snow_points, sand_points):

        counts = {
            KIND_RAIN: len(rain_points),
            KIND_SNOW: len(snow_points),
            KIND_SAND: len(sand_points),
        }
        
        HYSTERESIS_MARGIN = 1.15  # The new type must be at least 15% "heavier."
        
        current = getattr(self, '_dominant_weather_kind', KIND_RAIN)
        best_kind = max(counts, key=lambda k: counts[k])
        
        if best_kind == current:
            self._dominant_weather_kind = current
            return current
        
        if counts[best_kind] > counts[current] * HYSTERESIS_MARGIN:
            self._dominant_weather_kind = best_kind
            return best_kind
        
        return current
    
    def render(self):
        if self.current_layer > 0:
            darkness = max(5, 20 - self.current_layer)
            self.screen.fill((darkness // 3, darkness // 3, darkness))
        else:
            self.screen.fill((0, 0, 0))
        
        self.water_phase += 0.15
        self.lava_phase += 0.1
        
        
        self._light_dx, self._light_dy = self.sun.get_light_direction()
        self._sun_strength = max(0.15, self.sun.get_elevation())
        
        if self.isometric_mode:
            visible_tiles = self.get_visible_tiles_isometric()
            for depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y in visible_tiles:
                self.render_tile_isometric(x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y)
        else:
            visible_tiles = self.get_visible_tiles_topdown()
            for x, y, screen_x, screen_y in visible_tiles:
                self.render_tile_topdown(x, y, screen_x, screen_y)
        
        self.visible_tiles_count = len(visible_tiles)
        
        pixels_per_tile = (self.world.tile_size * self.current_zoom if self.isometric_mode
                           else self.world.tile_size * self.topdown_zoom)
        

        if self.current_layer == 0:
            size = self.world.chunk_size
            chunk_bounds = (
                self.current_chunk_x * size, self.current_chunk_y * size,
                (self.current_chunk_x + 1) * size, (self.current_chunk_y + 1) * size,
            )
            self.lighting.render(self.screen, self.world_to_screen, pixels_per_tile, chunk_bounds=chunk_bounds)
        

        if self.current_layer == 0:
            chunk_area = self._compute_chunk_screen_area(visible_tiles)
            if chunk_area is not None:
                kind, data = chunk_area
                if kind == 'polygon':
                    self.weather.set_area_polygon(data)
                else:
                    self.weather.set_area(*data)

                half_tile = self.world.tile_size * self.current_zoom / 2
                quarter_tile = half_tile / 2
                rain_points, snow_points, sand_points = self._compute_biome_landing_points(visible_tiles)
                self.weather.set_biome_landing_points(
                    rain_points, snow_points, sand_points,
                    jitter_x=half_tile * 0.5,
                    jitter_y=quarter_tile * 0.5,
                )

                dominant_kind = self._update_dominant_weather_kind(rain_points, snow_points, sand_points)
                self.weather.set_dominant_kind(dominant_kind)
                self.weather.render(self.screen)
                
                sound_biome = self._update_dominant_sound_biome(visible_tiles)
                self.sound.set_dominant_biome(sound_biome)
                weather_sound_map = {KIND_RAIN: WEATHER_RAIN, KIND_SNOW: WEATHER_SNOW, KIND_SAND: WEATHER_SAND}
                self.sound.set_weather(weather_sound_map.get(dominant_kind), self.weather.get_intensity())
            else:
                self.sound.set_dominant_biome(None)
                self.sound.set_weather(None, 0.0)
        else:
            self.sound.set_dominant_biome(None)
            self.sound.set_weather(None, 0.0)
        
        if self.show_info:
            self.render_info()
        
        if self.show_minimap:
            self.render_minimap()
        
        pygame.display.flip()
    
    def render_info(self):
        panel_rect = pygame.Rect(10, 10, 450, 500)
        
        if not hasattr(self, 'info_panel'):
            self.info_panel = pygame.Surface((panel_rect.width, panel_rect.height), pygame.SRCALPHA)
        
        self.info_panel.fill((0, 0, 0, 180))
        self.screen.blit(self.info_panel, panel_rect)
        
        mouse_x, mouse_y = pygame.mouse.get_pos()
        world_x, world_y = self.screen_to_world(mouse_x, mouse_y)
        
        tile_x = int(world_x / self.world.tile_size)
        tile_y = int(world_y / self.world.tile_size)
        
        visible_percent = (self.visible_tiles_count / self.total_tiles) * 100
        fps = int(self.clock.get_fps())
        
        #mode_text = "isometric" if self.isometric_mode else "top view"
        #mode_color = (100, 255, 100) if self.isometric_mode else (100, 200, 255)   (not used)
        
        layer_text = "surface"
        layer_color = (100, 255, 100)
        
        camera_tile_x = self.world_camera_x / self.world.tile_size
        camera_tile_y = self.world_camera_y / self.world.tile_size
        
        zoom_text = f"ZOOM: {self.current_zoom:.2f}x{' (fix)' if not self.isometric_mode else ''}"
        
        chunk_count = len(self.world.loader.loaded)
        total_chunks = self.world.num_chunks_x * self.world.num_chunks_y
        
        info_lines = [
            f"FPS: {fps}",
            f"click [I] hide info",
            f"",
            f"camera: ({int(self.world_camera_x)}, {int(self.world_camera_y)})",
            f"camera: ({camera_tile_x:.1f}, {camera_tile_y:.1f})",
            f"chunk: ({self.current_chunk_x}, {self.current_chunk_y})",
            f"chunk_loaded: {chunk_count}/{total_chunks} chunks",
            zoom_text,
            f"Grid: {'ON' if self.show_grid else 'OFF'} [G]",
            f"Map: {'ON' if self.show_minimap else 'OFF'} [M]",
            f"",

            f"",
            f"Weather: {self.weather.get_status_text()}density: {int(self.weather.get_density()*100)}%",
            f"Sun: {self.sun.get_status_text()} [T pause, ,/. time]",
            f"",

            f"",
            f"lights: {self.lighting.count()} (grid [G])",
            f"Sound: {self.sound.get_status_text()} vol {int(self.sound.get_master_volume()*100)}% [-/=]",
            f"",
            f"visible: {self.visible_tiles_count}",
            f"({visible_percent:.1f}%)",
            f"",
        ]
        
        if 0 <= tile_x < self.world.width and 0 <= tile_y < self.world.height:
            if self.current_layer == 0:
                tile_type = self.world.get_tile_type(tile_x, tile_y)
                type_names = {v: k for k, v in self.world.tile_types.items()}
                type_name = type_names[tile_type][:12]
            else:
                chunk = self.world.get_chunk(self.current_chunk_x, self.current_chunk_y)
                if chunk is not None and self.current_layer in chunk['cave_tile_maps']:
                    local_x = tile_x % self.world.chunk_size
                    local_y = tile_y % self.world.chunk_size
                    cave_type = chunk['cave_tile_maps'][self.current_layer][local_x, local_y]
                    type_names = {v: k for k, v in self.world.cave_tile_types.items()}
                    type_name = type_names[cave_type][:12]
                else:
                    type_name = "??? "
            
            height = self.world.get_height(tile_x, tile_y)
            chunk = self.world.get_chunk(self.current_chunk_x, self.current_chunk_y)
            cave_density = 0
            if chunk is not None and self.current_layer > 0 and self.current_layer in chunk['cave_density_maps']:
                local_x = tile_x % self.world.chunk_size
                local_y = tile_y % self.world.chunk_size
                cave_density = chunk['cave_density_maps'][self.current_layer][local_x, local_y]
            
            info_lines.extend([
                f"",
                f"({tile_x}, {tile_y})",
                f"{type_name}",
                f"h:{height:.2f}",
            ])
            
            if self.current_layer > 0:
                info_lines.append(f"cave:{cave_density:.2f}")
        
        y_offset = 20
        for line in info_lines:
            if line:
                if "mode:" in line:
                    color = mode_color # (not used)
                elif "layer:" in line:
                    color = layer_color
                elif "FPS:" in line:
                    color = (0, 255, 0) if fps >= 50 else ((255, 255, 0) if fps >= 30 else (255, 0, 0))
                elif "grid:" in line:
                    color = (200, 200, 0) if self.show_grid else (100, 100, 100)
                elif "mini map:" in line:
                    color = (200, 200, 0) if self.show_minimap else (100, 100, 100)
                elif "click map" in line:
                    color = (150, 150, 255)
                else:
                    color = (255, 255, 255)
                
                text = self.font.render(line, True, color)
                self.screen.blit(text, (20, y_offset))
            y_offset += 23 if line else 10
    
    def world_to_screen(self, tile_x, tile_y):
        if self.isometric_mode:
            iso_camera_x, iso_camera_y = self.get_isometric_camera_from_world()
            iso_x, iso_y = self.world.cartesian_to_isometric(tile_x, tile_y)
            screen_x = (iso_x - iso_camera_x) * self.current_zoom + self.screen_width // 2
            screen_y = (iso_y - iso_camera_y) * self.current_zoom + self.screen_height // 2
            return screen_x, screen_y
        else:
            screen_x = (tile_x * self.world.tile_size - self.world_camera_x) * self.topdown_zoom + self.screen_width // 2
            screen_y = (tile_y * self.world.tile_size - self.world_camera_y) * self.topdown_zoom + self.screen_height // 2
            return screen_x, screen_y
    
    def screen_to_world(self, screen_x, screen_y):
        if self.isometric_mode:
            iso_camera_x, iso_camera_y = self.get_isometric_camera_from_world()
            
            iso_x = (screen_x - self.screen_width // 2) / self.current_zoom + iso_camera_x
            iso_y = (screen_y - self.screen_height // 2) / self.current_zoom + iso_camera_y
            
            tile_x, tile_y = self.world.isometric_to_cartesian(iso_x, iso_y)
            return tile_x * self.world.tile_size, tile_y * self.world.tile_size
        else:
            world_x = (screen_x - self.screen_width // 2) / self.topdown_zoom + self.world_camera_x
            world_y = (screen_y - self.screen_height // 2) / self.topdown_zoom + self.world_camera_y
            return world_x, world_y
    
    def select_tile_at_screen(self, screen_x, screen_y):
        world_px_x, world_px_y = self.screen_to_world(screen_x, screen_y)
        tile_x = int(world_px_x // self.world.tile_size)
        tile_y = int(world_px_y // self.world.tile_size)
        
        if 0 <= tile_x < self.world.width and 0 <= tile_y < self.world.height:
            new_tile = (tile_x, tile_y)
            if new_tile != self.selected_tile:
                self.selected_tile = new_tile
                print(f"tile selected: ({tile_x}, {tile_y})")
    
    
    
    def regenerate_world(self):
        self.world = IsometricWorld(
            width=self.world.width,
            height=self.world.height,
            chunk_size=self.world.chunk_size,
            tile_size=self.world.tile_size
        )
        self.iso_coords.clear()
        self._gl_mesh_key = None
        self.world_camera_x = self.world.width * self.world.tile_size // 2
        self.world_camera_y = self.world.height * self.world.tile_size // 2
        self.target_world_camera_x = self.world_camera_x
        self.target_world_camera_y = self.world_camera_y
        self.current_chunk_x = int(self.world_camera_x / self.world.tile_size) // self.world.chunk_size
        self.current_chunk_y = int(self.world_camera_y / self.world.tile_size) // self.world.chunk_size
        self.preload_chunks()
        print(f"world recreated! Seed: {self.world.seed}")
    
    def run(self):
        """Main cycle"""
        running = True
        show_help = False
        
        while running:
            self.dt = self.clock.get_time() / 16.67
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key == pygame.K_F11:
                        pygame.display.toggle_fullscreen()
                    elif event.key == pygame.K_g:
                        self.show_grid = not self.show_grid
                    elif event.key == pygame.K_i:
                        self.show_info = not self.show_info
                    elif event.key == pygame.K_m:
                        self.show_minimap = not self.show_minimap
                    elif event.key == pygame.K_r:
                        self.regenerate_world()
                    elif event.key == pygame.K_c and pygame.key.get_mods() & pygame.KMOD_CTRL:
                        self.world_camera_x = self.world.width * self.world.tile_size // 2
                        self.world_camera_y = self.world.height * self.world.tile_size // 2
                        self.target_world_camera_x = self.world_camera_x
                        self.target_world_camera_y = self.world_camera_y
                        self.target_isometric_zoom = 1.5
                    elif event.key == pygame.K_F1:
                        show_help = not show_help
                    elif event.key == pygame.K_F2:
                        self.weather.toggle_rain()
                    elif event.key == pygame.K_LEFTBRACKET:
                        self.weather.set_density(max(0.0, self.weather.get_density() - 0.1))
                    elif event.key == pygame.K_RIGHTBRACKET:
                        self.weather.set_density(min(1.0, self.weather.get_density() + 0.1))
                    elif event.key == pygame.K_t:
                        self.sun.toggle_pause()
                    elif event.key == pygame.K_COMMA:
                        self.sun.set_time(self.sun.get_time_of_day() - 1.0 / 24.0)
                    elif event.key == pygame.K_PERIOD:
                        self.sun.set_time(self.sun.get_time_of_day() + 1.0 / 24.0)
                    elif event.key == pygame.K_MINUS:
                        self.sound.set_master_volume(self.sound.get_master_volume() - 0.1)
                    elif event.key == pygame.K_EQUALS:
                        self.sound.set_master_volume(self.sound.get_master_volume() + 0.1)
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        clicked_minimap = False
                        if self.show_minimap:
                            clicked_minimap = self.handle_minimap_click(event.pos[0], event.pos[1])
                        if not clicked_minimap and self.show_grid and self.selected_tile is not None:
                            
                            self.lighting.toggle_light_at(*self.selected_tile)
                    elif event.button == 4:
                        if self.isometric_mode:
                            self.target_isometric_zoom = min(3.0, self.target_isometric_zoom + 0.2)
                    elif event.button == 5:
                        if self.isometric_mode:
                            self.target_isometric_zoom = max(1.5, self.target_isometric_zoom - 0.2)
                elif event.type == pygame.MOUSEMOTION:
                    if event.buttons[2]:
                        self.target_world_camera_x -= event.rel[0] / self.current_zoom
                        self.target_world_camera_y -= event.rel[1] / self.current_zoom
                        
                        max_x = (self.world.width - 1) * self.world.tile_size
                        max_y = (self.world.height - 1) * self.world.tile_size
                        self.target_world_camera_x = max(0, min(max_x, self.target_world_camera_x))
                        self.target_world_camera_y = max(0, min(max_y, self.target_world_camera_y))
                    elif self.show_grid:
                        
                        over_minimap = (
                            self.show_minimap and
                            self.minimap_x <= event.pos[0] <= self.minimap_x + self.minimap_size and
                            self.minimap_y <= event.pos[1] <= self.minimap_y + self.minimap_size
                        )
                        if not over_minimap:
                            self.select_tile_at_screen(event.pos[0], event.pos[1])
            
            self.handle_input()
            self.weather.update(self.clock.get_time() / 1000.0)
            self.sun.update(self.clock.get_time() / 1000.0)
            self.sound.update(self.clock.get_time() / 1000.0)
            self.render()
            
            if show_help:
                self.render_help()
            
            self.clock.tick(60)
        
        # Останавливаем загрузчик
        self.world.loader.stop()
        pygame.quit()
        sys.exit()
    
    def render_help(self):
        help_surface = pygame.Surface((750, 800), pygame.SRCALPHA)
        help_surface.fill((0, 0, 0, 220))
        
        title = self.big_font.render("controls", True, (100, 255, 100))
        help_surface.blit(title, (300, 20))
        
        controls = [
            # :)
        ]
        
        y = 80
        for key, desc in controls:
            if key == "":
                y += 10
                continue
            
            key_color = (255, 255, 0) if key in ["V", "C / E"] else ((200, 200, 0) if key in ["G", "M"] else (100, 255, 100))
            desc_color = (255, 255, 0) if key in ["V", "C / E"] else (255, 255, 255)
            
            key_text = self.font.render(key, True, key_color)
            desc_text = self.font.render(desc, True, desc_color)
            
            help_surface.blit(key_text, (50, y))
            help_surface.blit(desc_text, (280, y))
            y += 30
        
        y += 10
        mode_text = f"РЕЖИМ: {'isometric' if self.isometric_mode else 'top view'}"
        mode_surface = self.font.render(mode_text, True, (100, 255, 100) if self.isometric_mode else (100, 200, 255))
        help_surface.blit(mode_surface, (50, y))
        
        y += 30
        layer_text = "surface"
        layer_surface = self.font.render(layer_text, True, (100, 255, 100))
        help_surface.blit(layer_surface, (50, y))
        
        self.screen.blit(help_surface, 
                        (self.screen_width // 2 - 375, 
                         self.screen_height // 2 - 400))


def main(): 
    # Generation world
    world = IsometricWorld(width=128, height=128, chunk_size=16, tile_size=16)
    renderer = DualViewRenderer(world)
    renderer.run()


if __name__ == "__main__":
    try:
        import numpy
        import pygame
    except ImportError as e:
        print(f"Error: {e}")
        print("\nDownload:")
        print("pip install numpy pygame")
        sys.exit(1)
    
    main()
