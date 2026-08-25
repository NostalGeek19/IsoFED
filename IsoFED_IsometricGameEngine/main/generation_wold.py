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
from lighting_system import LightingSystem, Light
from camera_rotation_system import CameraRotationSystem
from sound_system import (SoundSystem, BIOME_FOREST, BIOME_PLAINS,
                           BIOME_WATER, BIOME_MOUNTAINS,
                           WEATHER_RAIN, WEATHER_SNOW, WEATHER_SAND)
from thunderstorm_system import ThunderstormSystem
from fire_system import FireSystem
from explosion_system import ExplosionSystem
from rails_system import (RailsSystem, can_place_rail, can_place_cart,
                          get_rail_mirrored_at, needs_camera_flip)
from digging_system import DiggingSystem
from water_flow_system import WaterFlowSystem
from object_system import ObjectSystem
from fluid_cube_system import FluidCubeSystem
from texture_manager import TextureManager


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
            num_workers = max(1, cpu_count - 1) if cpu_count > 1 else 1
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
    def __init__(self, width=512, height=512, chunk_size=16, tile_size=64, seed=None):
        self.width = width
        self.height = height
        self.chunk_size = chunk_size
        self.tile_size = tile_size
        self.num_chunks_x = width // chunk_size
        self.num_chunks_y = height // chunk_size
        self.max_layers = 16
        
        # Perlin noise settings for natural generation
        self.seed = random.randint(0, 10000) if seed is None else int(seed)
        
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
            'savanna': 10,
            'snow': 11
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
            10: 2, # savanna -> cave_floor
            11: 1  # snow -> cave_wall
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
            (222, 184, 135), # 10: savanna
            (255, 255, 255)  # 11: snow
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
        half_tile = self.tile_size // 2
        iso_x = (world_x - world_y) * half_tile
        iso_y = (world_x + world_y) * (half_tile // 2)
        iso_coords = np.stack([iso_x, iso_y], axis=-1).astype(float)
        
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
            'iso_coords': iso_coords,
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
        

        hot = (temp > 0.3) & land
        tile_map[hot & (moisture < 0.2)] = self.tile_types['savanna']
        tile_map[hot & (moisture >= 0.2)] = self.tile_types['grassland']

        mild = (~hot) & land
        tile_map[mild] = self.tile_types['grassland']
        
        forest_cond = mild & (moisture >= -0.1)
        forest_height_cond = height > 0.08
        
        tile_map[forest_cond & forest_height_cond & (fertility > 0.7)] = self.tile_types['dense_forest']
        tile_map[forest_cond & forest_height_cond & (fertility <= 0.7)] = self.tile_types['forest']
        
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
        
        mountain_mask = np.isin(tile_map, [self.tile_types['mountains'],
                                            self.tile_types['high_peaks'],
                                            self.tile_types['snow']])
        hills_mask = tile_map == self.tile_types['hills']
        forest_mask = np.isin(tile_map, [self.tile_types['forest'],
                                          self.tile_types['dense_forest']])
        grass_mask = np.isin(tile_map, [self.tile_types['grassland'], self.tile_types['savanna']])
        
        detail_height[mountain_mask] += np.abs(ridge_noise[mountain_mask]) * 0.5
        detail_height[mountain_mask] += hill_noise[mountain_mask] * 0.8
        
        detail_height[hills_mask] += hill_noise[hills_mask] * 1.2
        detail_height[hills_mask] += ridge_noise[hills_mask] * 0.35
        
        detail_height[forest_mask] += forest_noise[forest_mask] * 0.5
        forest_bonus_mask = forest_mask & (forest_noise > 0.2)
        detail_height[forest_bonus_mask] += hill_noise[forest_bonus_mask] * 0.5
        
        detail_height[grass_mask] += hill_noise[grass_mask] * 0.25
        
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
        
        chunk['height_map'][forest_mask] += (forest_terrain[forest_mask] + 0.3) * 0.25
        forest_bonus_mask = forest_mask & (forest_terrain > 0.3)
        chunk['height_map'][forest_bonus_mask] += 0.08
        
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
        pygame.display.set_caption(f"World {world.width}x{world.height}")
        
        self.font = pygame.font.Font(None, 28)
        self.small_font = pygame.font.Font(None, 22)
        self.big_font = pygame.font.Font(None, 48)


        self.corner_label_text = "Public-release version v25.08.2026"
        self.corner_label_color = (255, 255, 255)
        
        self.current_layer = 0
        
        # A Camera at the Center of the World
        self.world_camera_x = world.width * world.tile_size // 2
        self.world_camera_y = world.height * world.tile_size // 2
        self.target_world_camera_x = self.world_camera_x
        self.target_world_camera_y = self.world_camera_y

        # Camera rotation (90° steps) — separate module, camera_rotation_system.py.
        # Toggled with the middle mouse button (scroll-wheel click).
        self.camera_rotation = CameraRotationSystem()

        self._rotation_snapshot = None
        self._rotation_fade = 0.0
        self.ROTATION_FADE_DURATION = 0.18  # секунд
        
        # Current camera chunk
        self.current_chunk_x = int(self.world_camera_x / world.tile_size) // world.chunk_size
        self.current_chunk_y = int(self.world_camera_y / world.tile_size) // world.chunk_size
        
        # For a smooth transition
        self.is_transitioning = False
        self.chunk_transition = 1.0
        self.old_chunk_data = None
        

        self.isometric_zoom = 1.5
        self.target_isometric_zoom = 1.5
        self.current_zoom = self.isometric_zoom
        
        self.show_grid = False

        self.selected_tile = None
        self.selection_anchor = None   
        self.selected_tiles = []       
        self.MAX_SELECTION_SIZE = 6    
        self._drag_paint_active = False
        self._drag_paint_last_tile = None
        self._alt_drag_reference_height = 0
        self.side_build_mode = False
        self.show_info = False
        self.show_object_picker = False  
        self.show_minimap = False
        
        self.clock = pygame.time.Clock()
        self.dt = 1.0
        
        # The weather mechanic (rain) has been moved to a separate module, `weather_system.py`.
        self.weather = WeatherSystem(self.screen_width, self.screen_height, seed=world.seed)
        
        # Sun mechanics (day/night cycle)
        self.sun = SunSystem(self.screen_width, self.screen_height, start_time=0.45)
        
        # The manual lighting mechanic (flashlights) is a separate module: `lighting_system.py`.
        self.lighting = LightingSystem()

        # Placeable objects (wooden cube and future props)
        self.objects = ObjectSystem()
        # Digging tiles (holes) — separate module, digging_system.py. Water
        # flowing into holes near shallow_water is handled separately by
        # water_flow_system.py.
        self.digging = DiggingSystem()
        self.water_flow = WaterFlowSystem(self.digging)
        # water_cube / lava_cube spreading, containment, and evaporation
        # — separate module, fluid_cube_system.py.
        self.fluids = FluidCubeSystem(on_tile_changed=self._sync_object_lights_at)

        self._placement_modes = ('light', 'object', 'dig')
        self.placement_mode = 'light'   # cycled with [O]
        
        self._object_light_tiles = {}

        self._tree_regrow_timers = {}   # (tile_x, tile_y)
        self.TREE_REGROW_DELAY_RANGE = (240.0, 300.0)
        
        # Sound is a separate module, sound_system.py: biome ambient sound
        self.sound = SoundSystem()
        self._sound_biome_category = None

        # Forest fires — separate module, fire_system.py. Lightning strikes
        # roll a chance to ignite a tree; burning trees spread to adjacent
        # trees after a delay.
        self.fire = FireSystem(sound_system=self.sound)

        # Bomb detonation — separate module, explosion_system.py. Hover a
        # placed 'bomb' object and press E to detonate it.
        self.explosions = ExplosionSystem(sound_system=self.sound,
                                           find_bombs_fn=self._find_bombs_within,
                                           remove_bomb_fn=self._remove_bomb_at,
                                           destroy_objects_fn=self._destroy_objects_within)

        self.rails = RailsSystem(sound_system=self.sound)


        self._bomb_fuse_timers = {}   # (tile_x, tile_y) 
        self.BOMB_FUSE_DELAY_RANGE = (4.0, 5.0)

        # Thunderstorm mechanic (lightning during rain)
        self.storm = ThunderstormSystem(self.lighting, self.sound, storm_chance=0.25, debug=True,
                                         on_strike=self._on_lightning_strike)
        self._last_storm_bounds = None
        
        # Custom per-tile textures — separate module, texture_manager.py.
        self.texture_manager = TextureManager()

        self._tint_cache = {}
        self._biome_id_to_name = {v: k for k, v in world.tile_types.items()}
        
        self._GRASS_OVERLAY_TEXTURE_NAMES = {
            'dense_forest': 'grass_dense_forest',
        }
        
        self._TREE_OVERLAY_TEXTURE_NAMES = {
            'savanna': 'savanna_sand',
            'beach': 'beach_sand',
            'hills': 'hills_t',
            'mountains': 'mountains_t',
            'snow': 'snow_t',
            'high_peaks': 'snow_peaks',
        }

        self.flower_density = 0.12
        self._flower_variants = self.texture_manager.discover_flowers()
        
        self.stone_density = 0.12
        self._STONE_BIOMES = ('savanna', 'hills')
        self._stone_variants_by_biome = {
            biome: self.texture_manager.discover_stones(biome) for biome in self._STONE_BIOMES
        }
        
        self.tree_density = 0.5
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
        is_default = self.camera_rotation.is_default_orientation()
        key = f"{chunk_x},{chunk_y},{'0' if is_default else self.camera_rotation.get_cache_key()}"
        if key not in self.iso_coords:
            chunk = self.world.get_chunk(chunk_x, chunk_y)
            if is_default and chunk is not None and 'iso_coords' in chunk:
                self.iso_coords[key] = chunk['iso_coords']
            else:
                size = self.world.chunk_size
                xx, yy = np.meshgrid(np.arange(size), np.arange(size), indexing='ij')
                world_x = xx + chunk_x * size
                world_y = yy + chunk_y * size
                vx, vy = self.camera_rotation.to_view_space(world_x, world_y)
                half_tile = self.world.tile_size // 2
                iso_x = (vx - vy) * half_tile
                iso_y = (vx + vy) * (half_tile // 2)
                computed = np.stack([iso_x, iso_y], axis=-1).astype(float)
                if len(self.iso_coords) > 200:
                    self.iso_coords.clear()
                self.iso_coords[key] = computed
        return self.iso_coords[key]
    
    def handle_input(self):
        keys = pygame.key.get_pressed()
        dt = self.dt
        
        speed = 15 * dt / self.current_zoom
        
        if keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]:
            speed *= 3
        
        move_x = move_y = 0
        
        if not self.rails.is_locked():
            if keys[pygame.K_LEFT] or keys[pygame.K_a]:
                move_x = -speed
            if keys[pygame.K_RIGHT] or keys[pygame.K_d]:
                move_x = speed
            if keys[pygame.K_UP] or keys[pygame.K_w]:
                move_y = -speed
            if keys[pygame.K_DOWN] or keys[pygame.K_s]:
                move_y = speed
        
        delta_world_x, delta_world_y = self.camera_rotation.to_world_space(move_x, move_y)

        self.target_world_camera_x += delta_world_x
        self.target_world_camera_y += delta_world_y
        
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
        
        self.isometric_zoom += (self.target_isometric_zoom - self.isometric_zoom) * 0.15
        self.current_zoom = self.isometric_zoom
    
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
        vx, vy = self.camera_rotation.to_view_space(center_tile_x, center_tile_y)
        return self.world.cartesian_to_isometric(vx, vy)
    
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
        
      
        screen_x_arr = (iso_coords[:, :, 0] - iso_camera_x) * self.current_zoom + self.screen_width // 2
        screen_y_arr = (iso_coords[:, :, 1] - iso_camera_y) * self.current_zoom + self.screen_height // 2
        visible_mask = ((screen_x_arr + half_tile > 0) & (screen_x_arr - half_tile < self.screen_width) &
                         (screen_y_arr + quarter_tile > 0) & (screen_y_arr - quarter_tile < self.screen_height))
        xs, ys = np.nonzero(visible_mask)
        for x, y in zip(xs.tolist(), ys.tolist()):
            vx, vy = self.camera_rotation.to_view_space(x, y)
            visible_tiles.append((vx + vy, x, y, screen_x_arr[x, y], screen_y_arr[x, y], 1.0,
                                   self.current_chunk_x, self.current_chunk_y))
        
        # We add tiles from the old chunk for a smooth transition.
        if self.is_transitioning and self.old_chunk_data is not None:
            alpha = 1.0 - self.chunk_transition
            old_chunk_x = self.old_chunk_data['chunk_x']
            old_chunk_y = self.old_chunk_data['chunk_y']
            old_iso_coords = self.get_chunk_iso_coords(old_chunk_x, old_chunk_y)
            
            old_screen_x_arr = (old_iso_coords[:, :, 0] - iso_camera_x) * self.current_zoom + self.screen_width // 2
            old_screen_y_arr = (old_iso_coords[:, :, 1] - iso_camera_y) * self.current_zoom + self.screen_height // 2
            old_visible_mask = ((old_screen_x_arr + half_tile > 0) & (old_screen_x_arr - half_tile < self.screen_width) &
                                 (old_screen_y_arr + quarter_tile > 0) & (old_screen_y_arr - quarter_tile < self.screen_height))
            old_xs, old_ys = np.nonzero(old_visible_mask)
            for x, y in zip(old_xs.tolist(), old_ys.tolist()):
                vx, vy = self.camera_rotation.to_view_space(x, y)
                visible_tiles.append((vx + vy + 10000, x, y, old_screen_x_arr[x, y], old_screen_y_arr[x, y], alpha,
                                       old_chunk_x, old_chunk_y))
        
        visible_tiles.sort(key=lambda t: t[0])
        return visible_tiles
    
    def _deterministic_fraction(self, world_x, world_y, salt=0):
        h = (world_x * 73856093) ^ (world_y * 19349663) ^ (self.world.seed * 83492791) ^ (salt * 2654435761)
        h &= 0xffffffff
        return (h % 100000) / 100000.0
    
    def _tile_has_flower(self, world_x, world_y):
        return self._deterministic_fraction(world_x, world_y, salt=1) < self.flower_density
    
    def _tile_flower_variant(self, world_x, world_y, variants):
        if not variants:
            return None
        index = int(self._deterministic_fraction(world_x, world_y, salt=2) * len(variants))
        index = min(index, len(variants) - 1)
        return variants[index]
    
    def _find_bombs_within(self, center_x, center_y, radius):
        r_int = int(math.ceil(radius))
        result = []
        for dx in range(-r_int, r_int + 1):
            for dy in range(-r_int, r_int + 1):
                if dx == 0 and dy == 0:
                    continue
                if math.sqrt(dx * dx + dy * dy) > radius:
                    continue
                tx, ty = center_x + dx, center_y + dy
                if self.objects.get_top_object_type(tx, ty) == 'bomb':
                    result.append((tx, ty))
        return result

    def _remove_bomb_at(self, tile_x, tile_y):
        self.objects.remove_top_object_at(tile_x, tile_y)
        self._sync_object_lights_at(tile_x, tile_y)

    def _update_bomb_fuses(self, dt):
        for (fx, fy) in self.fire.get_burning_tiles():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    key = (fx + dx, fy + dy)
                    if key in self._bomb_fuse_timers:
                        continue
                    if self.objects.get_top_object_type(key[0], key[1]) == 'bomb':
                        self._bomb_fuse_timers[key] = random.uniform(*self.BOMB_FUSE_DELAY_RANGE)

        if not self._bomb_fuse_timers:
            return

        expired = []
        for key, remaining in self._bomb_fuse_timers.items():
            remaining -= dt
            if remaining <= 0:
                expired.append(key)
            else:
                self._bomb_fuse_timers[key] = remaining

        for key in expired:
            del self._bomb_fuse_timers[key]
            if self.objects.get_top_object_type(key[0], key[1]) == 'bomb':
                self._remove_bomb_at(*key)
                self.explosions.detonate(*key)

    def _destroy_objects_within(self, center_x, center_y, radius):
        r_int = int(math.ceil(radius))
        for dx in range(-r_int, r_int + 1):
            for dy in range(-r_int, r_int + 1):
                if math.sqrt(dx * dx + dy * dy) > radius:
                    continue
                tx, ty = center_x + dx, center_y + dy

                if self.objects.has_object_at(tx, ty) and self.objects.get_top_object_type(tx, ty) != 'bomb':
                    self.objects.remove_all_at(tx, ty)
                    self._sync_object_lights_at(tx, ty)
                    if not self.objects.has_object_at(tx, ty):
                        self._start_tree_regrow_timer_if_needed(tx, ty)

                if self._tile_has_tree(tx, ty):
                    self._start_tree_regrow_timer_if_needed(tx, ty)

    def _sync_object_lights_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        stack = self.objects.get_stack_with_levels(tile_x, tile_y)

        light_obj_type = None
        light_config = None
        light_level = 0
        for level, obj_type, _mirrored in reversed(stack):
            config = self.objects.get_light_config(obj_type)
            if config is not None:
                light_obj_type = obj_type
                light_config = config
                light_level = level
                break

        if light_config is not None:
            self.lighting.lights[key] = Light(
                key[0], key[1],
                color=light_config.get('color', (255, 200, 120)),
                radius=light_config.get('radius', 4.0),
                intensity=light_config.get('intensity', 1.0),
                grounded=(light_level == 0),
            )
            self._object_light_tiles[key] = light_obj_type
        elif key in self._object_light_tiles:
            self.lighting.lights.pop(key, None)
            del self._object_light_tiles[key]

    def _update_object_light_flicker(self):
        if not self._object_light_tiles:
            return
        for key, obj_type in self._object_light_tiles.items():
            config = self.objects.get_light_config(obj_type)
            if not config:
                continue
            flicker_amp = config.get('flicker', 0.0)
            if not flicker_amp:
                continue
            light = self.lighting.lights.get(key)
            if light is None:
                continue
            base_intensity = config.get('intensity', 1.0)
            phase = (key[0] * 12.9898 + key[1] * 78.233) % (2 * math.pi)
            light.intensity = base_intensity + flicker_amp * math.sin(self.lava_phase + phase)

    def _get_tinted_sprite(self, key_prefix, raw_surface, color):
        qcolor = (color[0] & ~7, color[1] & ~7, color[2] & ~7)
        key = (key_prefix, qcolor)
        cached = self._tint_cache.get(key)
        if cached is not None:
            return cached
        
        tinted = raw_surface.copy()
        tinted.fill((*qcolor, 255), special_flags=pygame.BLEND_RGBA_MULT)
        
        if len(self._tint_cache) > 800:
            self._tint_cache.clear()
        self._tint_cache[key] = tinted
        return tinted

    def _tiles_between(self, x0, y0, x1, y1):
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        points = []
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        x, y = x0, y0
        while True:
            if (x, y) != (x0, y0) and (x, y) != (x1, y1):
                points.append((x, y))
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy
        return points

    def _is_light_path_blocked(self, light_tile, target_tile):
        lx, ly = light_tile
        tx, ty = target_tile
        if lx == tx and ly == ty:
            return False
        for (x, y) in self._tiles_between(lx, ly, tx, ty):
            if self.objects.has_object_at(x, y):
                return True
        return False

    def _object_light_color(self, tile_x, tile_y):

        color = self.sun.apply_tint((255, 255, 255))

        light_r, light_g, light_b = self.lighting.get_tile_light_boost(
            tile_x, tile_y, occlusion_fn=self._is_light_path_blocked)
        storm_r, storm_g, storm_b = self.storm.get_light_boost(tile_x, tile_y)
        fire_r, fire_g, fire_b = self.fire.get_light_boost(tile_x, tile_y)

        return (
            min(255, int(color[0] + light_r + storm_r + fire_r)),
            min(255, int(color[1] + light_g + storm_g + fire_g)),
            min(255, int(color[2] + light_b + storm_b + fire_b)),
        )
    
    def _tile_has_stone(self, world_x, world_y):
        return self._deterministic_fraction(world_x, world_y, salt=3) < self.stone_density
    
    def _tile_stone_variant(self, world_x, world_y, variants):
        if not variants:
            return None
        index = int(self._deterministic_fraction(world_x, world_y, salt=4) * len(variants))
        index = min(index, len(variants) - 1)
        return variants[index]
    
    def _tile_has_tree(self, world_x, world_y):
        if self.fire.is_tree_suppressed(world_x, world_y):
            return False
        if (int(world_x), int(world_y)) in self._tree_regrow_timers:
            return False
        if self.objects.has_object_at(world_x, world_y):
            return False
        if self.digging.is_dug(world_x, world_y):
            return False
        return self._deterministic_fraction(world_x, world_y, salt=5) < self.tree_density

    def _tile_is_water(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return False

        tile_type = int(chunk['tile_map'][local_x, local_y])
        biome_name = self._biome_id_to_name.get(tile_type)
        return biome_name in ('deep_ocean', 'ocean', 'shallow_water')

    def _tile_is_ocean(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return False

        tile_type = int(chunk['tile_map'][local_x, local_y])
        biome_name = self._biome_id_to_name.get(tile_type)
        return biome_name in ('deep_ocean', 'ocean')

    def _tile_has_no_stack_object(self, tile_x, tile_y):
        top_type = self.objects.get_top_object_type(tile_x, tile_y)
        if top_type is None:
            return False
        return self.objects.is_no_stack_type(top_type)

    def _tile_blocks_new_object_placement(self, tile_x, tile_y):
        return (self._tile_blocks_object_placement(tile_x, tile_y)
                or self._tile_is_ocean(tile_x, tile_y)
                or self._tile_has_no_stack_object(tile_x, tile_y))

    def _tile_blocks_object_placement(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return False

        tile_type = int(chunk['tile_map'][local_x, local_y])
        biome_name = self._biome_id_to_name.get(tile_type)
        return biome_name == 'dense_forest' and self._tile_has_tree(tile_x, tile_y)

    def _tile_blocks_digging(self, tile_x, tile_y):
        if self.objects.has_object_at(tile_x, tile_y):
            return True
        if self._tile_blocks_object_placement(tile_x, tile_y):
            return True
        if self._tile_is_water(tile_x, tile_y):
            return True
        return False

    def _tile_blocks_rail_placement(self, tile_x, tile_y):
        if self._tile_blocks_object_placement(tile_x, tile_y):
            return True
        if self._tile_is_water(tile_x, tile_y):
            return True
        if self.digging.is_dug(tile_x, tile_y):
            return True
        return False

    def _rail_rotation_fn(self, obj_type, mirrored):
        return needs_camera_flip(obj_type, self.camera_rotation.get_rotation_steps())

    def _start_tree_regrow_timer_if_needed(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return

        tile_type = int(chunk['tile_map'][local_x, local_y])
        biome_name = self._biome_id_to_name.get(tile_type)
        if biome_name != 'dense_forest':
            return
        if self._deterministic_fraction(tile_x, tile_y, salt=5) >= self.tree_density:
            return  

        key = (tile_x, tile_y)
        if key in self._tree_regrow_timers:
            return 

        self._tree_regrow_timers[key] = random.uniform(*self.TREE_REGROW_DELAY_RANGE)

    def _update_tree_regrow_timers(self, dt):
        if not self._tree_regrow_timers:
            return
        expired = []
        for key, remaining in self._tree_regrow_timers.items():
            remaining -= dt
            if remaining <= 0:
                expired.append(key)
            else:
                self._tree_regrow_timers[key] = remaining
        for key in expired:
            del self._tree_regrow_timers[key]

    def _on_lightning_strike(self, tile_x, tile_y):
        self.fire.try_ignite_from_lightning(tile_x, tile_y, self._tile_blocks_object_placement)

    def _tile_is_shallow_water(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return False

        tile_type = int(chunk['tile_map'][local_x, local_y])
        return tile_type == self.world.tile_types['shallow_water']

    
    def render_tile_isometric(self, x, y, screen_x, screen_y, alpha=1.0, chunk_x=None, chunk_y=None):
        if chunk_x is None:
            chunk_x = self.current_chunk_x
        if chunk_y is None:
            chunk_y = self.current_chunk_y
        
        # For tiles from a chunk, we use world coordinates.
        world_x = chunk_x * self.world.chunk_size + x
        world_y = chunk_y * self.world.chunk_size + y
        
        chunk = self.world.get_chunk(chunk_x, chunk_y)
        
        if chunk is None:
            color = self.world.get_tile(world_x, world_y, self.current_layer)
        else:
            color = self.world.get_tile_color(chunk, x, y, self.current_layer)
        
        if len(color) > 3:
            color = color[:3]
        color = tuple(max(0, min(255, int(c))) for c in color)
        
        # We apply transparency for a smooth transition.
        if alpha < 1.0:
            bg_color = (10, 10, 20)
            color = tuple(int(c * alpha + bg * (1 - alpha)) for c, bg in zip(color, bg_color))
        
        # Effects for different tile types
        if self.current_layer == 0:
            size = self.world.chunk_size
            if chunk is not None:
                tile_type = int(chunk['tile_map'][x, y])
                height = float(chunk['height_map'][x, y])
            else:
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
                    h1 = height
                    if chunk is not None and x > 0 and y > 0:
                        h2 = float(chunk['height_map'][x - 1, y])
                        h3 = float(chunk['height_map'][x, y - 1])
                    else:
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
                    h1 = height
                    if chunk is not None and x > 0 and y > 0:
                        h2 = float(chunk['height_map'][x - 1, y])
                        h3 = float(chunk['height_map'][x, y - 1])
                    else:
                        h2 = self.world.get_height(world_x - 1, world_y)
                        h3 = self.world.get_height(world_x, world_y - 1)
                    dx = h1 - h2
                    dy = h1 - h3
                    facing = dx * self._light_dx + dy * self._light_dy
                    shadow = max(0, -facing * 20) * self._sun_strength
                    slope_highlight = max(0, facing * 20) * self._sun_strength
                    color = tuple(max(0, min(255, c - int(shadow) + int(slope_highlight))) for c in color)
            
            elif tile_type in [self.world.tile_types['forest'], 
                              self.world.tile_types['dense_forest']]:
                if height > 0.08:
                    canopy_height = (height - 0.08) * 40
                    color = tuple(min(255, c + int(canopy_height * 0.6)) for c in color)
            

            color = self.sun.apply_tint(color)
            
            
            light_r, light_g, light_b = self.lighting.get_tile_light_boost(
                world_x, world_y, occlusion_fn=self._is_light_path_blocked, for_ground=True)
            fire_r, fire_g, fire_b = self.fire.get_light_boost(world_x, world_y)
            light_r, light_g, light_b = light_r + fire_r, light_g + fire_g, light_b + fire_b
            if light_r or light_g or light_b:
                color = (
                    min(255, int(color[0] + light_r)),
                    min(255, int(color[1] + light_g)),
                    min(255, int(color[2] + light_b)),
                )

            if self.objects.has_object_at(world_x, world_y):
                stack_height = self.objects.get_stack_height(world_x, world_y)
                shadow_strength = min(0.55, 0.12 * stack_height)
                shadow_mult = 1.0 - shadow_strength
                color = tuple(max(0, int(c * shadow_mult)) for c in color)

            color = self.explosions.apply_darken(world_x, world_y, color)


            if self.digging.is_dug(world_x, world_y):
                color = self.digging.apply_darken(world_x, world_y, color)
                water_info = self.water_flow.get_water_color(world_x, world_y)
                if water_info is not None:
                    water_color, water_strength = water_info


                    lit_water_color = self.sun.apply_tint(water_color)
                    if light_r or light_g or light_b:
                        lit_water_color = (
                            min(255, int(lit_water_color[0] + light_r)),
                            min(255, int(lit_water_color[1] + light_g)),
                            min(255, int(lit_water_color[2] + light_b)),
                        )

                    ripple = math.sin((world_x + world_y) * 0.3 + self.water_phase) * 8 * water_strength
                    if ripple > 0:
                        lit_water_color = tuple(max(0, min(255, int(c + ripple))) for c in lit_water_color)

                    color = tuple(
                        int(color[i] * (1 - water_strength) + lit_water_color[i] * water_strength)
                        for i in range(3)
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
            if height > 0:
                height_offset = -int(height * 8 * zoom)
        
        points = [
            (screen_x, screen_y - quarter_tile + height_offset),
            (screen_x + half_tile, screen_y + height_offset),
            (screen_x, screen_y + quarter_tile + height_offset),
            (screen_x - half_tile, screen_y + height_offset)
        ]
        
        diamond_texture = None
        if self.current_layer == 0:
            biome_name = self._biome_id_to_name.get(tile_type)
            if biome_name is not None:
                diamond_texture = self.texture_manager.get_diamond_texture(biome_name, half_tile, quarter_tile)
        
        if diamond_texture is not None:
            tinted = self._get_tinted_sprite(('diamond', biome_name, half_tile, quarter_tile), diamond_texture, color)
            rect = tinted.get_rect(center=(screen_x, screen_y + height_offset))
            self.screen.blit(tinted, rect)
        else:
            pygame.draw.polygon(self.screen, color, points)
        
        if self.current_layer == 0 and biome_name is not None:
            grass_texture_name = self._GRASS_OVERLAY_TEXTURE_NAMES.get(biome_name, biome_name)
            grass_overlay = self.texture_manager.get_grass_overlay(grass_texture_name, half_tile * 2)
            if grass_overlay is not None:
                grass_color = color
                if biome_name == 'dense_forest':
                    darken = self.fire.get_grass_darken(world_x, world_y)
                    if darken > 0:
                        mult = 1.0 - 0.6 * darken
                        grass_color = tuple(max(0, int(c * mult)) for c in color)
                tinted_grass = self._get_tinted_sprite(('grass', grass_texture_name, half_tile * 2), grass_overlay, grass_color)
                grass_rect = tinted_grass.get_rect(midbottom=(screen_x, screen_y + quarter_tile + height_offset))
                self.screen.blit(tinted_grass, grass_rect)
            
            overlay_texture_name = self._TREE_OVERLAY_TEXTURE_NAMES.get(biome_name)
            if overlay_texture_name is not None:
                tree_overlay = self.texture_manager.get_tree_overlay(overlay_texture_name, half_tile * 2)
                if tree_overlay is not None:
                    tinted_tree = self._get_tinted_sprite(('tree', overlay_texture_name, half_tile * 2), tree_overlay, color)
                    tree_rect = tinted_tree.get_rect(midbottom=(screen_x, screen_y + quarter_tile + height_offset))
                    self.screen.blit(tinted_tree, tree_rect)
            
            if biome_name == 'dense_forest' and self._tile_has_tree(world_x, world_y):
                tree_sprite = self.texture_manager.get_tree_overlay('dense_forest', half_tile * 2)
                if tree_sprite is not None:
                    tinted_random_tree = self._get_tinted_sprite(('tree', 'dense_forest', half_tile * 2), tree_sprite, color)
                    random_tree_rect = tinted_random_tree.get_rect(midbottom=(screen_x, screen_y + quarter_tile + height_offset))
                    self.screen.blit(tinted_random_tree, random_tree_rect)
        
        if (self.current_layer == 0 and biome_name == 'grassland' and self._flower_variants
                and self._tile_has_flower(world_x, world_y)):
            variant = self._tile_flower_variant(world_x, world_y, self._flower_variants)

            flower_sprite = self.texture_manager.get_flower_texture(variant, half_tile * 2)
            if flower_sprite is not None:
                tinted_flower = self._get_tinted_sprite(('flower', variant, half_tile * 2), flower_sprite, color)
                flower_rect = tinted_flower.get_rect(midbottom=(screen_x, screen_y + quarter_tile + height_offset))
                self.screen.blit(tinted_flower, flower_rect)
        
        stone_variants = self._stone_variants_by_biome.get(biome_name) if biome_name in self._STONE_BIOMES else None
        if (self.current_layer == 0 and stone_variants
                and self._tile_has_stone(world_x, world_y)):
            variant = self._tile_stone_variant(world_x, world_y, stone_variants)

            stone_sprite = self.texture_manager.get_stone_texture(variant, half_tile * 2)
            if stone_sprite is not None:
                tinted_stone = self._get_tinted_sprite(('stone', variant, half_tile * 2), stone_sprite, color)
                stone_rect = tinted_stone.get_rect(midbottom=(screen_x, screen_y + quarter_tile + height_offset))
                self.screen.blit(tinted_stone, stone_rect)
        
        if self.show_grid:
            grid_color = (30, 30, 30) if self.current_layer > 0 else (60, 60, 60)
            pygame.draw.polygon(self.screen, grid_color, points, 1)

            if self.selected_tiles:
                is_selected = (world_x, world_y) in self.selected_tiles
            else:
                is_selected = self.selected_tile == (world_x, world_y)
            if is_selected:
                pygame.draw.polygon(self.screen, (255, 215, 0), points, 3)
    
    def _get_dynamic_hints(self):
        hints = []

        if self.rails.is_locked():
            hints.append("W/S ride / E release cart")
        elif self.show_grid and self.selected_tile is not None:
            top_type = self.objects.get_top_object_type(*self.selected_tile)
            if top_type == 'bomb':
                hints.append("E detonate bomb")
            elif top_type == 'cart':
                hints.append("E ride cart")

        if self.show_grid:
            hints.append(f"O placement: {self.placement_mode}  (G hide grid)")
            if self.placement_mode == 'object':
                hints.append("LMB place / RMB remove / TAB type / F mirror")
                hints.append("Ctrl+drag build / I picker" +
                              (" X side-build ON" if self.side_build_mode else " X side-build"))
            elif self.placement_mode == 'dig':
                hints.append("LMB dig / RMB fill hole")
            else:
                hints.append("LMB toggle lamp")
            hints.append("Shift+hover select area")
        else:
            hints.append("G show grid to build")

        hints.append("M minimap / Esc quit")
        return hints

    def render_dynamic_hints(self):
        hints = self._get_dynamic_hints()
        if not hints:
            return

        line_surfaces = [self.small_font.render(h, True, (225, 225, 225)) for h in hints]
        width = max(s.get_width() for s in line_surfaces) + 24
        height = sum(s.get_height() for s in line_surfaces) + 2 * (len(line_surfaces) - 1) + 12

        panel = pygame.Surface((width, height), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 130))

        y = 6
        for surface in line_surfaces:
            rect = surface.get_rect(topright=(width - 12, y))
            panel.blit(surface, rect)
            y += surface.get_height() + 2

        panel_rect = panel.get_rect(bottomright=(self.screen_width - 20, self.screen_height - 20))
        self.screen.blit(panel, panel_rect)

    def render_corner_label(self):
        text_surface = self.small_font.render(self.corner_label_text, True, self.corner_label_color)
        text_rect = text_surface.get_rect(topright=(self.screen_width - 20, 8))

        bg_rect = text_rect.inflate(16, 8)
        bg = pygame.Surface(bg_rect.size, pygame.SRCALPHA)
        bg.fill((0, 0, 0, 120))
        self.screen.blit(bg, bg_rect)
        self.screen.blit(text_surface, text_rect)

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
        
        view_width = (self.screen_width / self.current_zoom) / self.world.tile_size * 1.5
        view_height = (self.screen_height / self.current_zoom) / self.world.tile_size * 1.5
        
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
    
    # Biomes with snowfall (cold/high-altitude) and sandstorms (savanna).
    # Everything else (grass, forest, hills, water) uses rain.
    _SNOW_BIOME_NAMES = ('snow', 'high_peaks', 'mountains')
    _SAND_BIOME_NAMES = ('savanna',)

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
        'forest': BIOME_FOREST, 'dense_forest': BIOME_FOREST,
        'mountains': BIOME_MOUNTAINS, 'high_peaks': BIOME_MOUNTAINS, 'snow': BIOME_MOUNTAINS,
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
        
        chunk_cache = {}
        for depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y in visible_tiles:
            chunk_key = (tile_chunk_x, tile_chunk_y)
            chunk = chunk_cache.get(chunk_key, False)
            if chunk is False:
                chunk = self.world.get_chunk(tile_chunk_x, tile_chunk_y)
                chunk_cache[chunk_key] = chunk
            
            tile_type = int(chunk['tile_map'][x, y]) if chunk is not None else 0
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

    def _thunder_biome_at(self, tile_x, tile_y):
        size = self.world.chunk_size
        tile_x, tile_y = int(tile_x), int(tile_y)
        chunk_x, chunk_y = tile_x // size, tile_y // size
        local_x, local_y = tile_x % size, tile_y % size

        chunk = self.world.get_chunk(chunk_x, chunk_y)
        if chunk is None:
            return None

        tile_type = int(chunk['tile_map'][local_x, local_y])
        if tile_type in self._get_snow_biome_ids():
            return KIND_SNOW
        if tile_type in self._get_sand_biome_ids():
            return KIND_SAND
        return KIND_RAIN

    def _compute_biome_landing_points(self, visible_tiles):
        snow_ids = self._get_snow_biome_ids()
        sand_ids = self._get_sand_biome_ids()
        
        rain_points = []
        snow_points = []
        sand_points = []
        
        zoom = self.current_zoom
        chunk_cache = {}
        
        for depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y in visible_tiles:
            chunk_key = (tile_chunk_x, tile_chunk_y)
            chunk = chunk_cache.get(chunk_key, False)
            if chunk is False:
                chunk = self.world.get_chunk(tile_chunk_x, tile_chunk_y)
                chunk_cache[chunk_key] = chunk
            
            if chunk is not None:
                height_value = float(chunk['height_map'][x, y])
                tile_type = int(chunk['tile_map'][x, y])
            else:
                height_value = 0.0
                tile_type = 0
            
            height_offset = -int(height_value * 8 * zoom) if height_value > 0 else 0
            point = (screen_x, screen_y + height_offset)
            
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
        self._update_object_light_flicker()
        
        
        self._light_dx, self._light_dy = self.sun.get_light_direction()
        self._sun_strength = max(0.15, self.sun.get_elevation())
        
        pixels_per_tile = self.world.tile_size * self.current_zoom
        size = self.world.chunk_size
        
        visible_tiles = self.get_visible_tiles_isometric()
        for depth, x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y in visible_tiles:
            self.render_tile_isometric(x, y, screen_x, screen_y, alpha, tile_chunk_x, tile_chunk_y)
            if self.current_layer == 0:
                world_x = tile_chunk_x * size + x
                world_y = tile_chunk_y * size + y
                self.objects.render_at_tile(self.screen, self.world_to_screen, pixels_per_tile,
                                             world_x, world_y, light_fn=self._object_light_color,
                                             rotation_fn=self._rail_rotation_fn)
                if self.fire.is_burning_at(world_x, world_y):
                    self.fire.render(self.screen, self.world_to_screen, pixels_per_tile,
                                      chunk_bounds=(world_x, world_y, world_x + 1, world_y + 1))
        
        self.visible_tiles_count = len(visible_tiles)
        

        if self.current_layer == 0:
            chunk_bounds = (
                self.current_chunk_x * size, self.current_chunk_y * size,
                (self.current_chunk_x + 1) * size, (self.current_chunk_y + 1) * size,
            )
            self.sound.set_active_chunk_bounds(chunk_bounds)
            self.lighting.render(self.screen, self.world_to_screen, pixels_per_tile, chunk_bounds=chunk_bounds)
            self.explosions.render(self.screen, self.world_to_screen, pixels_per_tile, chunk_bounds=chunk_bounds)

            if (self.show_grid and self.placement_mode == 'object'
                    and self.selected_tile is not None):
                selected_type = self.objects.get_selected_type()
                for tile in self._get_selection_tiles():
                    if selected_type == 'rail':
                        blocked = not can_place_rail(self.objects, tile[0], tile[1],
                                                      self._tile_blocks_rail_placement)
                    elif selected_type == 'cart':
                        blocked = not can_place_cart(self.objects, tile[0], tile[1])
                    elif self.side_build_mode and self._max_neighbor_top_level(*tile) >= 0:
                        blocked = self._tile_blocks_object_placement(*tile)
                    else:
                        blocked = self._tile_blocks_new_object_placement(*tile)
                    self.objects.render_preview(self.screen, self.world_to_screen,
                                                 pixels_per_tile, *tile, blocked=blocked,
                                                 rotation_fn=self._rail_rotation_fn)

            self.fire.update(self.clock.get_time() / 1000.0, tree_at_fn=self._tile_blocks_object_placement)
            self.water_flow.update(self.clock.get_time() / 1000.0, water_neighbor_fn=self._tile_is_shallow_water)
            self.fluids.update(self.clock.get_time() / 1000.0, self.objects)
            self._update_tree_regrow_timers(self.clock.get_time() / 1000.0)
            self.explosions.update(self.clock.get_time() / 1000.0)
            self._update_bomb_fuses(self.clock.get_time() / 1000.0)
            self.rails.update(self.clock.get_time() / 1000.0, self.objects)
            if self.rails.is_locked():
                cam_tile_x, cam_tile_y = self.rails.get_camera_target_tile()
                self.target_world_camera_x = cam_tile_x * self.world.tile_size
                self.target_world_camera_y = cam_tile_y * self.world.tile_size
                self.world_camera_x = self.target_world_camera_x
                self.world_camera_y = self.target_world_camera_y
        

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

                self._last_storm_bounds = chunk_bounds
                storm_dt = self.clock.get_time() / 1000.0
                self.storm.update(
                    storm_dt,
                    weather_kind=weather_sound_map.get(dominant_kind),
                    weather_intensity=self.weather.get_intensity(),
                    camera_tile_bounds=chunk_bounds,
                    biome_at_fn=self._thunder_biome_at,
                    current_biome=KIND_RAIN,
                )
                self.storm.render(self.screen, self.world_to_screen, pixels_per_tile)
            else:
                self.sound.set_dominant_biome(None)
                self.sound.set_weather(None, 0.0)
                self.storm.update(self.clock.get_time() / 1000.0, weather_kind=None, weather_intensity=0.0)
        else:
            self.sound.set_dominant_biome(None)
            self.sound.set_weather(None, 0.0)
            self.storm.update(self.clock.get_time() / 1000.0, weather_kind=None, weather_intensity=0.0)
        
        if self._rotation_snapshot is not None and self._rotation_fade > 0.0:
            self._rotation_snapshot.set_alpha(int(255 * self._rotation_fade))
            self.screen.blit(self._rotation_snapshot, (0, 0))
            dt_seconds = self.clock.get_time() / 1000.0
            self._rotation_fade -= dt_seconds / self.ROTATION_FADE_DURATION
            if self._rotation_fade <= 0.0:
                self._rotation_fade = 0.0
                self._rotation_snapshot = None

        if self.show_info:
            self.render_info()
        
        if self.show_minimap:
            self.render_minimap()
        
        self.render_corner_label()
        self.render_dynamic_hints()
        
        if self.show_grid and self.show_object_picker:
            self.render_object_picker()
        
        pygame.display.flip()
    
    def render_object_picker(self):
        entries = self.objects.get_type_ids()
        if not entries:
            return

        slot_size = 56
        row_height = slot_size + 14
        padding = 14
        panel_width = 230
        panel_height = padding * 2 + 30 + row_height * len(entries)
        panel_x = 20
        panel_y = 20

        panel_key = (panel_width, panel_height)
        if not hasattr(self, '_object_picker_panel') or self._object_picker_panel_key != panel_key:
            self._object_picker_panel = pygame.Surface((panel_width, panel_height), pygame.SRCALPHA)
            self._object_picker_panel_key = panel_key
        self._object_picker_panel.fill((0, 0, 0, 180))
        self.screen.blit(self._object_picker_panel, (panel_x, panel_y))

        title = self.font.render("Objects [TAB / click]", True, (255, 255, 255))
        self.screen.blit(title, (panel_x + padding, panel_y + 8))

        selected_type = self.objects.get_selected_type()
        self._object_picker_slots = []  # (rect, obj_type)

        y = panel_y + 34
        for obj_type in entries:
            slot_rect = pygame.Rect(panel_x + padding, y, slot_size, slot_size)
            is_selected = (obj_type == selected_type)
            border_color = (255, 215, 0) if is_selected else (90, 90, 90)

            pygame.draw.rect(self.screen, (25, 25, 25), slot_rect)

            preview = self.objects.get_preview_texture(obj_type, slot_size - 10)
            if preview is not None:
                preview_rect = preview.get_rect(center=slot_rect.center)
                self.screen.blit(preview, preview_rect)
            else:
                placeholder_rect = slot_rect.inflate(-18, -18)
                pygame.draw.rect(self.screen, (191, 143, 90), placeholder_rect)
                pygame.draw.rect(self.screen, (140, 100, 62), placeholder_rect, 2)

            pygame.draw.rect(self.screen, border_color, slot_rect, 3 if is_selected else 1)

            label_text = self.objects.get_label_for(obj_type)
            label_color = (255, 230, 150) if is_selected else (200, 200, 200)
            label = self.font.render(label_text, True, label_color)
            self.screen.blit(label, (slot_rect.right + 12, slot_rect.centery - label.get_height() // 2))

            self._object_picker_slots.append((slot_rect, obj_type))
            y += row_height

    def handle_object_picker_click(self, mouse_x, mouse_y):
        if not (self.show_grid and self.show_object_picker):
            return False
        for rect, obj_type in getattr(self, '_object_picker_slots', []):
            if rect.collidepoint(mouse_x, mouse_y):
                self.objects.set_selected_type(obj_type)
                self.placement_mode = 'object'
                return True
        return False

    def render_info(self):
        panel_rect = pygame.Rect(10, 10, 450, 500)
        
        if not hasattr(self, 'info_panel'):
            self.info_panel = pygame.Surface((panel_rect.width, panel_rect.height), pygame.SRCALPHA)
        
        #self.info_panel.fill((0, 0, 0, 180))
        self.screen.blit(self.info_panel, panel_rect)
        
        mouse_x, mouse_y = pygame.mouse.get_pos()
        world_x, world_y = self.screen_to_world(mouse_x, mouse_y)
        
        tile_x = int(world_x / self.world.tile_size)
        tile_y = int(world_y / self.world.tile_size)
        
        visible_percent = (self.visible_tiles_count / self.total_tiles) * 100
        fps = int(self.clock.get_fps())
        
        layer_text = "surface"
        layer_color = (100, 255, 100)
        
        camera_tile_x = self.world_camera_x / self.world.tile_size
        camera_tile_y = self.world_camera_y / self.world.tile_size
        
        #zoom_text = f"zoom: {self.current_zoom:.2f}x"
        
        chunk_count = len(self.world.loader.loaded)
        total_chunks = self.world.num_chunks_x * self.world.num_chunks_y
        
        info_lines = [
            f"FPS: {fps}",
            f"click [I] hide info",
            f"cam.: ({int(self.world_camera_x)}, {int(self.world_camera_y)})",
            f"cam.: ({camera_tile_x:.1f}, {camera_tile_y:.1f})",
            f"ck.: ({self.current_chunk_x}, {self.current_chunk_y})",
            f"ckl.: {chunk_count}/{total_chunks} cks.",
            #zoom_text,
            f"Gd.: {'ON' if self.show_grid else 'OFF'} [G]",
            f"Map: {'ON' if self.show_minimap else 'OFF'} [M]",
            f"Wea.: {self.weather.get_status_text()}dens.: {int(self.weather.get_density()*100)}%",
            f"T: {self.sun.get_status_text()} [T pause, ,/. time]",
            f"Cam. rot.: {self.camera_rotation.get_status_text()} [hold middle mouse + move to rotate]",
            f"Stm.: {self.storm.get_status_text() or 'off'} chance {self.storm.get_storm_chance():.0%} [9/0, Y force, U strike]",
            f"Fire: {self.fire.get_status_text()} [L ignite selected tile]",
            f"Expl.: {self.explosions.get_status_text()} [E detonate bomb under cursor]",
            f"Rail.: {'riding cart' if self.rails.is_locked() else 'not riding'} [E lock/unlock cart, W/S direction]",
            f"Dig.: {self.digging.get_status_text()} | Water: {self.water_flow.get_status_text()} | "
            f"Fluids: {self.fluids.get_status_text()}",
            f"Tree: {len(self._tree_regrow_timers)} tile(s) waiting",
            f"l: {self.lighting.count()} (grid [G])",
            f"Obj.: {self.objects.get_status_text()} [O placement: {self.placement_mode}]",
            f"Sel.: {len(self._get_selection_tiles())} [{self.MAX_SELECTION_SIZE}x{self.MAX_SELECTION_SIZE}]",
            f"Sound: {self.sound.get_status_text()} vol {int(self.sound.get_master_volume()*100)}% [-/=]",
            f"vis.: {self.visible_tiles_count}",
            f"({visible_percent:.1f}%)",

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
                f"h:{height:.6f}",
            ])
            
            if self.current_layer > 0:
                info_lines.append(f"cave:{cave_density:.2f}")
        
        y_offset = 20
        for line in info_lines:
            if line:
                if "mode:" in line:
                    color = None
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
        iso_camera_x, iso_camera_y = self.get_isometric_camera_from_world()
        vx, vy = self.camera_rotation.to_view_space(tile_x, tile_y)
        iso_x, iso_y = self.world.cartesian_to_isometric(vx, vy)
        screen_x = (iso_x - iso_camera_x) * self.current_zoom + self.screen_width // 2
        screen_y = (iso_y - iso_camera_y) * self.current_zoom + self.screen_height // 2
        return screen_x, screen_y
    
    def screen_to_world(self, screen_x, screen_y):
        iso_camera_x, iso_camera_y = self.get_isometric_camera_from_world()
        
        iso_x = (screen_x - self.screen_width // 2) / self.current_zoom + iso_camera_x
        iso_y = (screen_y - self.screen_height // 2) / self.current_zoom + iso_camera_y
        
        vx, vy = self.world.isometric_to_cartesian(iso_x, iso_y)
        tile_x, tile_y = self.camera_rotation.to_world_space(vx, vy)
        return tile_x * self.world.tile_size, tile_y * self.world.tile_size
    
    def _max_neighbor_top_level(self, tile_x, tile_y):
        best = -1
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                h = self.objects.get_stack_height(tile_x + dx, tile_y + dy)
                if h - 1 > best:
                    best = h - 1
        return best

    def _place_object_side_aware(self, tiles):
        selected_type = self.objects.get_selected_type()
        remaining = list(tiles)
        for _ in range(len(remaining) + 1):
            progressed = False
            still_remaining = []
            for t in remaining:
                reference_level = self._max_neighbor_top_level(*t)
                if reference_level < 0:
                    still_remaining.append(t)
                    continue
                if self._tile_blocks_object_placement(*t) or self._tile_has_no_stack_object(*t):
                    continue
                if self.objects.place_object_at(*t, level=reference_level):
                    self._sync_object_lights_at(*t)
                    self.objects.play_place_sound(selected_type, self.sound)
                    progressed = True
            remaining = still_remaining
            if not progressed:
                break

        for t in remaining:
            if self._tile_blocks_new_object_placement(*t):
                continue
            if self.objects.place_object_at(*t):
                self._sync_object_lights_at(*t)
                self.objects.play_place_sound(selected_type, self.sound)

    def _apply_placement_action_at(self, tiles):
        if self.placement_mode == 'object':
            selected_type = self.objects.get_selected_type()
            if selected_type == 'rail':
                for t in tiles:
                    if can_place_rail(self.objects, t[0], t[1], self._tile_blocks_rail_placement):
                        self.objects.place_object_at(*t, obj_type='rail')
                        self._sync_object_lights_at(*t)
                        self.objects.play_place_sound('rail', self.sound)
                    else:
                        print(f"[Rails] can't place rail at {t}: tile is occupied, water, or a dug hole")
            elif selected_type == 'cart':
                for t in tiles:
                    if can_place_cart(self.objects, t[0], t[1]):
                        mirrored = get_rail_mirrored_at(self.objects, t[0], t[1])
                        self.objects.place_object_at(*t, obj_type='cart', mirrored=mirrored)
                        self._sync_object_lights_at(*t)
                        self.objects.play_place_sound('cart', self.sound)
                        print(f"[Rails] cart placed at {t}")
                    else:
                        print(f"[Rails] can't place cart at {t}: needs an empty rail there first "
                              f"(top object: {self.objects.get_top_object_type(*t)!r})")
            elif self.objects.get_fluid_kind(selected_type) is not None:
                fluid_kind = self.objects.get_fluid_kind(selected_type)
                for t in tiles:
                    if self._tile_blocks_new_object_placement(*t):
                        continue
                    if self.fluids.pour(self.objects, t[0], t[1], fluid_kind):
                        self._sync_object_lights_at(*t)
                        self.objects.play_place_sound(selected_type, self.sound)
            elif self.side_build_mode:
                self._place_object_side_aware(tiles)
            else:
                for t in tiles:
                    if not self._tile_blocks_new_object_placement(*t):
                        self.objects.place_object_at(*t)
                        self._sync_object_lights_at(*t)
                        self.objects.play_place_sound(selected_type, self.sound)
        elif self.placement_mode == 'dig':
            for t in tiles:
                if not self._tile_blocks_digging(*t):
                    self.digging.dig_at(*t)
        else:
            for t in tiles:
                self.lighting.toggle_light_at(*t)

    def _apply_horizontal_build_at_height(self, tiles, reference_height):
        if self.placement_mode != 'object' or reference_height <= 0:
            return
        selected_type = self.objects.get_selected_type()
        for t in tiles:
            if self._tile_blocks_new_object_placement(*t):
                continue
            placed_any = False
            while self.objects.get_stack_height(*t) < reference_height:
                if not self.objects.place_object_at(*t):
                    break  
                self._sync_object_lights_at(*t)
                placed_any = True
            if placed_any:
                self.objects.play_place_sound(selected_type, self.sound)

    def select_tile_at_screen(self, screen_x, screen_y):
        world_px_x, world_px_y = self.screen_to_world(screen_x, screen_y)
        tile_x = int(world_px_x // self.world.tile_size)
        tile_y = int(world_px_y // self.world.tile_size)
        
        if 0 <= tile_x < self.world.width and 0 <= tile_y < self.world.height:
            new_tile = (tile_x, tile_y)

            keys = pygame.key.get_pressed()
            shift_held = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]

            if shift_held:
                if self.selection_anchor is None:
                    self.selection_anchor = self.selected_tile or new_tile
                self.selected_tile = new_tile
                self.selected_tiles = self._compute_selection_rect(self.selection_anchor, new_tile)
            else:
                if self.selection_anchor is not None or self.selected_tiles:
                    self.selection_anchor = None
                    self.selected_tiles = []
                if new_tile != self.selected_tile:
                    self.selected_tile = new_tile
                    print(f"tile selected: ({tile_x}, {tile_y})")

    def _compute_selection_rect(self, anchor, current):
        ax, ay = anchor
        bx, by = current

        min_x, max_x = (ax, bx) if ax <= bx else (bx, ax)
        min_y, max_y = (ay, by) if ay <= by else (by, ay)

        limit = self.MAX_SELECTION_SIZE
        if max_x - min_x + 1 > limit:
            if bx >= ax:
                max_x = min_x + limit - 1
            else:
                min_x = max_x - limit + 1
        if max_y - min_y + 1 > limit:
            if by >= ay:
                max_y = min_y + limit - 1
            else:
                min_y = max_y - limit + 1

        return [(x, y) for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1)]

    def _get_selection_tiles(self):
        if self.selected_tiles:
            return self.selected_tiles
        if self.selected_tile is not None:
            return [self.selected_tile]
        return []
    
    
    
    def regenerate_world(self):
        self.world = IsometricWorld(
            width=self.world.width,
            height=self.world.height,
            chunk_size=self.world.chunk_size,
            tile_size=self.world.tile_size
        )
        self.iso_coords.clear()
        self._gl_mesh_key = None

        self.objects.remove_all()
        self._object_light_tiles.clear()
        self.lighting.remove_all()
        self.digging.remove_all()
        self.fire.remove_all()
        self.explosions.remove_all()
        self.rails.cart = None
        self._tree_regrow_timers.clear()
        self._bomb_fuse_timers.clear()

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
                    elif event.key == pygame.K_l:
                        if self.show_grid and self.selected_tile is not None:
                            if self._tile_is_water(*self.selected_tile):
                                print(f"[Fire] can't ignite {self.selected_tile}: it's water")
                            elif self.fire.ignite(*self.selected_tile):
                                print(f"[Fire] ignited {self.selected_tile}")
                            else:
                                print(f"[Fire] {self.selected_tile} is already burning right now")
                    elif event.key == pygame.K_e:
                        if self.rails.is_locked():
                            self.rails.toggle_lock(self.objects, 0, 0)
                            print("[Rails] camera released")
                        elif self.show_grid and self.selected_tile is not None:
                            if self.objects.get_top_object_type(*self.selected_tile) == 'bomb':
                                self._remove_bomb_at(*self.selected_tile)
                                self.explosions.detonate(*self.selected_tile)
                            else:
                                top_type = self.objects.get_top_object_type(*self.selected_tile)
                                locked = self.rails.toggle_lock(self.objects, *self.selected_tile)
                                if locked:
                                    print(f"[Rails] camera locked onto cart at {self.selected_tile}")
                                else:
                                    print(f"[Rails] no cart under cursor at {self.selected_tile} "
                                          f"(top object there: {top_type!r}) — nothing to lock onto")
                        else:
                            print("[Rails] E pressed, but grid is off or no tile selected — "
                                  "turn on the grid (G) and hover a cart first")
                    elif event.key == pygame.K_w:
                        if self.rails.is_locked():
                            chosen = self.rails.set_direction(1, self.objects)
                            if chosen is not None:
                                print(f"[Rails] direction set: +1 (W) -> offset {chosen}")
                            else:
                                print("[Rails] W pressed, but no connected rail found nearby to move onto")
                        else:
                            print("[Rails] W pressed, but not riding a cart (press E over a cart first)")
                    elif event.key == pygame.K_s:
                        if self.rails.is_locked():
                            chosen = self.rails.set_direction(-1, self.objects)
                            if chosen is not None:
                                print(f"[Rails] direction set: -1 (S) -> offset {chosen}")
                            else:
                                print("[Rails] S pressed, but no connected rail found nearby to move onto")
                        else:
                            print("[Rails] S pressed, but not riding a cart (press E over a cart first)")
                    elif event.key == pygame.K_f:
                        if self.show_grid and self.placement_mode == 'object':
                            if self.selected_tile is not None and self.objects.has_object_at(*self.selected_tile):
                                self.objects.flip_top_object_at(*self.selected_tile)
                            else:
                                self.objects.toggle_mirror_next()
                    elif event.key == pygame.K_o:
                        idx = self._placement_modes.index(self.placement_mode)
                        self.placement_mode = self._placement_modes[(idx + 1) % len(self._placement_modes)]
                        print(f"place mode: {self.placement_mode}")
                    elif event.key == pygame.K_x:
                        self.side_build_mode = not self.side_build_mode
                        print(f"side build: {'on' if self.side_build_mode else 'off'}")
                    elif event.key == pygame.K_TAB:
                        if self.placement_mode == 'object':
                            self.objects.cycle_selected_type()
                            print(f"object selected: {self.objects.get_selected_label()}")
                    elif event.key == pygame.K_i:
                        if self.show_grid:
                            self.show_object_picker = not self.show_object_picker
                        else:
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
                    elif event.key == pygame.K_y:
                        self.storm.force_start_storm()
                    elif event.key == pygame.K_u:
                        if self._last_storm_bounds is not None:
                            self.storm.force_strike(self._last_storm_bounds, self._thunder_biome_at, KIND_RAIN)
                    elif event.key == pygame.K_9:
                        self.storm.set_storm_chance(self.storm.get_storm_chance() - 0.1)
                        print(f"chance: {self.storm.get_storm_chance():.0%}")
                    elif event.key == pygame.K_0:
                        self.storm.set_storm_chance(self.storm.get_storm_chance() + 0.1)
                        print(f"chance: {self.storm.get_storm_chance():.0%}")
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        clicked_picker = self.handle_object_picker_click(*event.pos)
                        clicked_minimap = False
                        if not clicked_picker and self.show_minimap:
                            clicked_minimap = self.handle_minimap_click(event.pos[0], event.pos[1])
                        if not clicked_picker and not clicked_minimap and self.show_grid and self.selected_tile is not None:
                            mods = pygame.key.get_mods()
                            if mods & pygame.KMOD_ALT:
                                existing_height = self.objects.get_stack_height(*self.selected_tile)
                                if self.placement_mode == 'object' and existing_height > 0:
                                    self._alt_drag_reference_height = existing_height
                                    self._drag_paint_active = True
                                    self._drag_paint_last_tile = self.selected_tile
                            else:
                                self._apply_placement_action_at(self._get_selection_tiles())
                                self._drag_paint_active = True
                                self._drag_paint_last_tile = self.selected_tile
                                self._alt_drag_reference_height = self.objects.get_stack_height(*self.selected_tile)
                    elif event.button == 2:
                        self.camera_rotation.start_drag()
                    elif event.button == 3:
                        if self.show_grid and self.selected_tile is not None:
                            tiles = self._get_selection_tiles()
                            if self.placement_mode == 'object':
                                for t in tiles:
                                    self.objects.remove_top_object_at(*t)
                                    self._sync_object_lights_at(*t)
                                    if not self.objects.has_object_at(*t):
                                        self._start_tree_regrow_timer_if_needed(*t)
                            elif self.placement_mode == 'dig':
                                for t in tiles:
                                    self.digging.fill_dirt_at(*t)
                                    if not self.digging.is_dug(*t):
                                        self._start_tree_regrow_timer_if_needed(*t)
                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        self._drag_paint_active = False
                        self._drag_paint_last_tile = None
                    elif event.button == 2:
                        self.camera_rotation.stop_drag()
                elif event.type == pygame.MOUSEMOTION:
                    if self.camera_rotation.is_dragging():
                        steps_before = self.camera_rotation.get_rotation_steps()
                        self.camera_rotation.accumulate_drag(event.rel[0])
                        if self.camera_rotation.get_rotation_steps() != steps_before:
                            self._rotation_snapshot = self.screen.copy()
                            self._rotation_fade = 1.0
                    elif event.buttons[2]:
                        rel_x = -event.rel[0] / self.current_zoom
                        rel_y = -event.rel[1] / self.current_zoom
                        delta_world_x, delta_world_y = self.camera_rotation.to_world_space(rel_x, rel_y)
                        self.target_world_camera_x += delta_world_x
                        self.target_world_camera_y += delta_world_y
                        
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
                            prev_tile = self._drag_paint_last_tile
                            self.select_tile_at_screen(event.pos[0], event.pos[1])
                            if (self._drag_paint_active and event.buttons[0]
                                    and self.selected_tile is not None
                                    and self.selected_tile != prev_tile):
                                mods = pygame.key.get_mods()
                                if self.selected_tiles:
                                    drag_tiles = self._get_selection_tiles()
                                elif prev_tile is not None:
                                    drag_tiles = self._tiles_between(*prev_tile, *self.selected_tile)
                                    drag_tiles.append(self.selected_tile)
                                else:
                                    drag_tiles = [self.selected_tile]

                                self._drag_paint_last_tile = self.selected_tile
                                if mods & pygame.KMOD_ALT:
                                    self._apply_horizontal_build_at_height(
                                        drag_tiles, self._alt_drag_reference_height)
                                elif mods & pygame.KMOD_CTRL:
                                    self._apply_placement_action_at(drag_tiles)
            
            self.handle_input()
            self.weather.update(self.clock.get_time() / 1000.0)
            self.sun.update(self.clock.get_time() / 1000.0)
            self.sound.update(self.clock.get_time() / 1000.0)
            self.render()
            
            
            
            self.clock.tick(60)
        

        self.world.loader.stop()
        pygame.quit()
        sys.exit()
    

def _get_seed_from_args_or_env():
    seed = None

    for i, arg in enumerate(sys.argv):
        if arg in ("--seed", "-s") and i + 1 < len(sys.argv):
            seed = sys.argv[i + 1]
            break
        if arg.startswith("--seed="):
            seed = arg.split("=", 1)[1]
            break

    if seed is None:
        seed = os.environ.get("WORLD_SEED")

    if seed is None or str(seed).strip() == "":
        return None

    try:
        return int(seed)
    except ValueError:
        # Allow non-numeric seeds (e.g. words) by hashing them into an int
        return abs(hash(str(seed))) % 10_000_000


def main():
    # Generation world
    seed = _get_seed_from_args_or_env()
    world = IsometricWorld(width=512, height=512, chunk_size=16, tile_size=64, seed=seed)
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
