import os
import math
import pygame


TEXTURE_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'bioms'),
    '/textures/bioms',
]

GRASS_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'grass'),
    '/textures/grass',
]

TREE_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'trees'),
    '/textures/trees',
]

SUPPORTED_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp')

FLOWER_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'flowers'),
    '/textures/flowers',
]


_MISSING = object()


def _describe_dir_contents(directories):
    parts = []
    for directory in directories:
        if os.path.isdir(directory):
            try:
                entries = sorted(os.listdir(directory))
            except OSError as e:
                parts.append(f"{directory} (couldn't list it: {e})")
                continue
            if entries:
                parts.append(f"{directory} actually contains: {', '.join(entries)}")
            else:
                parts.append(f"{directory} exists but is empty")
        else:
            parts.append(f"{directory} (this folder doesn't exist)")
    return " | ".join(parts)


class TextureManager:

    def __init__(self, search_dirs=None, grass_search_dirs=None, tree_search_dirs=None, flower_search_dirs=None):
        self.search_dirs = search_dirs or TEXTURE_SEARCH_DIRS
        self.grass_search_dirs = grass_search_dirs or GRASS_SEARCH_DIRS
        self.tree_search_dirs = tree_search_dirs or TREE_SEARCH_DIRS
        self.flower_search_dirs = flower_search_dirs or FLOWER_SEARCH_DIRS
        self._raw_cache = {}       # biome_name -> pygame.Surface | _MISSING
        self._diamond_cache = {}   # (biome_name, w, h) -> pygame.Surface (diamond, per-pixel alpha)
        self._square_cache = {}    # (biome_name, size) -> pygame.Surface (for top-down mode)
        self._grass_raw_cache = {}    # biome_name -> pygame.Surface | _MISSING
        self._grass_scaled_cache = {}  # (biome_name, width_px) -> pygame.Surface
        self._tree_raw_cache = {}    # biome_name -> pygame.Surface | _MISSING
        self._tree_scaled_cache = {}  # (biome_name, width_px) -> pygame.Surface
        self._flower_variants = None   # list of variant names, discovered lazily
        self._flower_raw_cache = {}    # variant_name -> pygame.Surface
        self._flower_scaled_cache = {}  # (variant_name, width_px) -> pygame.Surface

    def _find_file(self, biome_name):
        for directory in self.search_dirs:
            for ext in SUPPORTED_EXTENSIONS:
                path = os.path.join(directory, biome_name + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _load_raw(self, biome_name):
        if biome_name in self._raw_cache:
            cached = self._raw_cache[biome_name]
            return None if cached is _MISSING else cached

        path = self._find_file(biome_name)
        if path is None:
            self._raw_cache[biome_name] = _MISSING
            return None

        try:
            surface = pygame.image.load(path).convert_alpha()
        except Exception as e:
            searched = " or ".join(self.search_dirs)
            print(f"Texture manager: failed to load '{biome_name}' from {path}: {e} "
                  f"(searched {searched}) — falling back to the flat biome color")
            self._raw_cache[biome_name] = _MISSING
            return None

        self._raw_cache[biome_name] = surface
        return surface

    def has_texture(self, biome_name):
        return self._load_raw(biome_name) is not None


    def get_diamond_texture(self, biome_name, half_tile, quarter_tile):
        raw = self._load_raw(biome_name)
        if raw is None:
            return None

        target_w = max(2, int(round(half_tile * 2)))
        target_h = max(2, int(round(quarter_tile * 2)))
        key = (biome_name, target_w, target_h)

        cached = self._diamond_cache.get(key)
        if cached is not None:
            return cached

        diamond = self._square_to_diamond(raw, target_w, target_h)

        # Zoom changes continuously while animating, so this cache can pick
        # up a lot of transient sizes — keep it from growing forever.
        if len(self._diamond_cache) > 300:
            self._diamond_cache.clear()
        self._diamond_cache[key] = diamond
        return diamond

    @staticmethod
    def _square_to_diamond(square_surface, target_w, target_h):
        side = max(2, int(round(target_w / math.sqrt(2))) + 1)
        scaled = pygame.transform.smoothscale(square_surface, (side, side))
        rotated = pygame.transform.rotate(scaled, 45)

        return pygame.transform.smoothscale(rotated, (target_w, target_h))


    def _find_grass_file(self, biome_name):
        for directory in self.grass_search_dirs:
            for ext in SUPPORTED_EXTENSIONS:
                path = os.path.join(directory, biome_name + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _load_raw_grass(self, biome_name):
        if biome_name in self._grass_raw_cache:
            cached = self._grass_raw_cache[biome_name]
            return None if cached is _MISSING else cached

        path = self._find_grass_file(biome_name)
        if path is None:
            searched = " or ".join(self.grass_search_dirs)
            names = "/".join(biome_name + ext for ext in SUPPORTED_EXTENSIONS)
            print(f"Texture manager: no grass overlay found for '{biome_name}' "
                  f"(looked for {names} in {searched}) — drawing the flat tile only, no blades on top\n"
                  f"    -> {_describe_dir_contents(self.grass_search_dirs)}")
            self._grass_raw_cache[biome_name] = _MISSING
            return None

        try:
            surface = pygame.image.load(path).convert_alpha()
        except Exception as e:
            searched = " or ".join(self.grass_search_dirs)
            print(f"Texture manager: failed to load grass overlay '{biome_name}' from {path}: {e} "
                  f"(searched {searched}) — no grass overlay will be drawn for this biome")
            self._grass_raw_cache[biome_name] = _MISSING
            return None

        print(f"Texture manager: loaded grass overlay for '{biome_name}' from {path}")
        self._grass_raw_cache[biome_name] = surface
        return surface

    def has_grass_overlay(self, biome_name):
        return self._load_raw_grass(biome_name) is not None

    def get_grass_overlay(self, biome_name, width_px):
        raw = self._load_raw_grass(biome_name)
        if raw is None:
            return None

        width_px = max(2, int(round(width_px)))
        key = (biome_name, width_px)
        cached = self._grass_scaled_cache.get(key)
        if cached is not None:
            return cached

        raw_w, raw_h = raw.get_size()
        scale = width_px / raw_w
        height_px = max(2, int(round(raw_h * scale)))
        scaled = pygame.transform.smoothscale(raw, (width_px, height_px))

        if len(self._grass_scaled_cache) > 300:
            self._grass_scaled_cache.clear()
        self._grass_scaled_cache[key] = scaled
        return scaled


    def _find_tree_file(self, biome_name):
        for directory in self.tree_search_dirs:
            for ext in SUPPORTED_EXTENSIONS:
                path = os.path.join(directory, biome_name + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _load_raw_tree(self, biome_name):
        if biome_name in self._tree_raw_cache:
            cached = self._tree_raw_cache[biome_name]
            return None if cached is _MISSING else cached

        path = self._find_tree_file(biome_name)
        if path is None:
            searched = " or ".join(self.tree_search_dirs)
            names = "/".join(biome_name + ext for ext in SUPPORTED_EXTENSIONS)
            print(f"Texture manager: no tree overlay found for '{biome_name}' "
                  f"(looked for {names} in {searched}) — drawing the flat tile only, no trees on top\n"
                  f"    -> {_describe_dir_contents(self.tree_search_dirs)}")
            self._tree_raw_cache[biome_name] = _MISSING
            return None

        try:
            surface = pygame.image.load(path).convert_alpha()
        except Exception as e:
            searched = " or ".join(self.tree_search_dirs)
            print(f"Texture manager: failed to load tree overlay '{biome_name}' from {path}: {e} "
                  f"(searched {searched}) — no tree overlay will be drawn for this biome")
            self._tree_raw_cache[biome_name] = _MISSING
            return None

        print(f"Texture manager: loaded tree overlay for '{biome_name}' from {path}")
        self._tree_raw_cache[biome_name] = surface
        return surface

    def has_tree_overlay(self, biome_name):
        return self._load_raw_tree(biome_name) is not None

    def get_tree_overlay(self, biome_name, width_px):
        raw = self._load_raw_tree(biome_name)
        if raw is None:
            return None

        width_px = max(2, int(round(width_px)))
        key = (biome_name, width_px)
        cached = self._tree_scaled_cache.get(key)
        if cached is not None:
            return cached

        raw_w, raw_h = raw.get_size()
        scale = width_px / raw_w
        height_px = max(2, int(round(raw_h * scale)))
        scaled = pygame.transform.smoothscale(raw, (width_px, height_px))

        if len(self._tree_scaled_cache) > 300:
            self._tree_scaled_cache.clear()
        self._tree_scaled_cache[key] = scaled
        return scaled


    def discover_flowers(self):
        if self._flower_variants is not None:
            return self._flower_variants

        found = {}  # variant_name -> full path (first match wins, same as biome textures)
        for directory in self.flower_search_dirs:
            if not os.path.isdir(directory):
                continue
            for entry in sorted(os.listdir(directory)):
                name, ext = os.path.splitext(entry)
                if name.startswith('flower_') and ext.lower() in SUPPORTED_EXTENSIONS and name not in found:
                    found[name] = os.path.join(directory, entry)

        if not found:
            searched = " or ".join(self.flower_search_dirs)
            print(f"Texture manager: no flower sprites found (looked for flower_*.png/.jpg/.jpeg/.bmp "
                  f"in {searched}) — no flowers will be drawn\n"
                  f"    -> {_describe_dir_contents(self.flower_search_dirs)}")

        self._flower_variants = sorted(found)
        self._flower_paths = found
        return self._flower_variants

    def _load_raw_flower(self, variant_name):
        if variant_name in self._flower_raw_cache:
            cached = self._flower_raw_cache[variant_name]
            return None if cached is _MISSING else cached

        self.discover_flowers()  # make sure self._flower_paths exists
        path = self._flower_paths.get(variant_name)
        if path is None:
            self._flower_raw_cache[variant_name] = _MISSING
            return None

        try:
            surface = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"Texture manager: failed to load flower sprite '{variant_name}' from {path}: {e}")
            self._flower_raw_cache[variant_name] = _MISSING
            return None

        print(f"Texture manager: loaded flower sprite '{variant_name}' from {path}")
        self._flower_raw_cache[variant_name] = surface
        return surface

    def get_flower_texture(self, variant_name, width_px):
        raw = self._load_raw_flower(variant_name)
        if raw is None:
            return None

        width_px = max(2, int(round(width_px)))
        key = (variant_name, width_px)
        cached = self._flower_scaled_cache.get(key)
        if cached is not None:
            return cached

        raw_w, raw_h = raw.get_size()
        scale = width_px / raw_w
        height_px = max(2, int(round(raw_h * scale)))
        scaled = pygame.transform.smoothscale(raw, (width_px, height_px))

        if len(self._flower_scaled_cache) > 300:
            self._flower_scaled_cache.clear()
        self._flower_scaled_cache[key] = scaled
        return scaled

    def get_square_texture(self, biome_name, size_px):
        raw = self._load_raw(biome_name)
        if raw is None:
            return None

        size_px = max(2, int(round(size_px)))
        key = (biome_name, size_px)
        cached = self._square_cache.get(key)
        if cached is not None:
            return cached

        scaled = pygame.transform.smoothscale(raw, (size_px, size_px))
        if len(self._square_cache) > 300:
            self._square_cache.clear()
        self._square_cache[key] = scaled
        return scaled


    def clear_cache(self):
        self._diamond_cache.clear()
        self._square_cache.clear()
        self._grass_scaled_cache.clear()
        self._tree_scaled_cache.clear()
        self._flower_scaled_cache.clear()

    def reload(self):
        self._raw_cache.clear()
        self._grass_raw_cache.clear()
        self._tree_raw_cache.clear()
        self._flower_raw_cache.clear()
        self._flower_variants = None
        self.clear_cache()