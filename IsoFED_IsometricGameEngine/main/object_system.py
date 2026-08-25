import os
import math
import pygame
import numpy as np


OBJECT_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'textures', 'objects'),
    '/textures/objects',
]

SUPPORTED_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp')

OBJECT_SOUND_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sound', 'objects'),
    '/sound/objects',
]
SUPPORTED_SOUND_EXTENSIONS = ('.wav', '.mp3', '.ogg')
PLACE_SOUND_SAMPLE_RATE = 22050

_MISSING = object()

MAX_STACK_HEIGHT = 10


OBJECT_TYPES = {
    'wooden_cube': {
        'label': 'Wooden Cube',
        'texture': 'wooden_cube',
        'width_scale': 1.0,
        'level_height_scale': 0.5,
        'sound_material': 'wood',
    },
    'stone_cube': {
        'label': 'Stone Cube',
        'texture': 'stone_cube',
        'width_scale': 1.0,
        'level_height_scale': 0.5,
        'sound_material': 'stone',
    },
    'water_cube': {
        'label': 'Water Cube',
        'texture': 'water_cube',
        'width_scale': 1.0,
        'level_height_scale': 0.5,
        'sound_material': 'liquid',
        'fluid': 'water',
    },
    'lava_cube': {
        'label': 'Lava Cube',
        'texture': 'lava_cube',
        'width_scale': 1.0,
        'level_height_scale': 0.5,
        'sound_material': 'lava',
        'fluid': 'lava',

        'light': {'color': (255, 110, 40), 'radius': 4.5, 'intensity': 1.2, 'flicker': 0.35},
    },
    'lamp_cube': {
        'label': 'Lamp Cube',
        'texture': 'lamp_cube',
        'width_scale': 1.0,
        'level_height_scale': 0.5,
        'sound_material': 'glass',
        'light': {'color': (255, 215, 140), 'radius': 5.5, 'intensity': 1.3, 'flicker': 0.0},
    },
    #'wooden_stairs': {
        #'label': 'Wooden Stairs',
        #'texture': 'wooden_stairs',
        #'width_scale': 1.0,
        #'level_height_scale': 0.5,
        #'sound_material': 'wood',
   # },
    #'stone_stairs': {
        #'label': 'Stone Stairs',
        #'texture': 'stone_stairs',
        #'width_scale': 1.0,
        #'level_height_scale': 0.5,
        #'sound_material': 'stone',
    #},
    'bomb': {
        'label': 'Bomb',
        'texture': 'bomb',
        'width_scale': 0.85,
        'level_height_scale': 0.5,
        'sound_material': 'metal',
    },
    'rail': {
        'label': 'Rail',
        'texture': 'rail',
        'width_scale': 1.0,
        'level_height_scale': 0.1,
        'no_stack': True,  
        'sound_material': 'metal',
    },
    'cart': {
        'label': 'Cart',
        'texture': 'cart',
        'width_scale': 0.9,
        'level_height_scale': 0.15,
        'no_stack': True,  
        'sound_material': 'metal',
    },
    # Mirroring (for stairs and similar non-symmetrical objects)
    
}

DEFAULT_OBJECT_TYPE = 'wooden_cube'


def _make_place_sound_signal(material, seed):
    rng = np.random.RandomState(seed)
    n = int(PLACE_SOUND_SAMPLE_RATE * rng.uniform(0.15, 0.25))
    t = np.arange(n) / PLACE_SOUND_SAMPLE_RATE

    presets = {
        'wood':   dict(tone_freq=170,  tone_decay=16, noise_win=6,  noise_decay=22, tone_amt=0.55),
        'stone':  dict(tone_freq=90,   tone_decay=10, noise_win=3,  noise_decay=16, tone_amt=0.35),
        'metal':  dict(tone_freq=480,  tone_decay=7,  noise_win=2,  noise_decay=14, tone_amt=0.75),
        'glass':  dict(tone_freq=1400, tone_decay=20, noise_win=2,  noise_decay=25, tone_amt=0.65),
        'liquid': dict(tone_freq=240,  tone_decay=12, noise_win=10, noise_decay=10, tone_amt=0.20),
        'lava':   dict(tone_freq=130,  tone_decay=6,  noise_win=8,  noise_decay=8,  tone_amt=0.15),
    }
    p = presets.get(material, presets['wood'])

    noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    win = max(1, int(p['noise_win']))
    kernel = np.ones(win, dtype=np.float32) / win
    noise = np.convolve(noise, kernel, mode='same').astype(np.float32)
    noise_env = np.exp(-t * p['noise_decay']).astype(np.float32)

    tone = np.sin(2 * math.pi * p['tone_freq'] * t).astype(np.float32)
    tone_env = np.exp(-t * p['tone_decay']).astype(np.float32)

    signal = noise * noise_env * (1.0 - p['tone_amt']) + tone * tone_env * p['tone_amt']
    peak = float(np.abs(signal).max()) + 1e-6
    signal = (signal / peak) * 0.85
    return signal.astype(np.float32)


class PlacedObject:
    __slots__ = ('tile_x', 'tile_y', 'obj_type', 'level', 'mirrored')

    def __init__(self, tile_x, tile_y, obj_type, level, mirrored=False):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.obj_type = obj_type
        self.level = level   
        self.mirrored = mirrored   


class ObjectSystem:

    def __init__(self, search_dirs=None, max_stack_height=MAX_STACK_HEIGHT):
        self.search_dirs = search_dirs or OBJECT_SEARCH_DIRS
        self.max_stack_height = max_stack_height
        self.stacks = {}   # (tile_x, tile_y) -> {level: (obj_type, mirrored)}
        self.fluid_fill = {}   # (tile_x, tile_y, level) -> fraction 0..1; missing == 1.0 (full/solid)
        self.selected_type = DEFAULT_OBJECT_TYPE
        self.mirror_next = False   

        self._raw_cache = {}      # obj_type -> Surface | _MISSING
        self._scaled_cache = {}   # (obj_type, width_px) -> Surface
        self._tint_cache = {}     # (obj_type, width_px, quantized_color) -> Surface

        self._place_sound_cache = {}          # obj_type -> pygame.mixer.Sound
        self._place_sound_missing_logged = set()

    # ------------------------------------------------------------------

    def set_selected_type(self, obj_type):
        if obj_type in OBJECT_TYPES:
            self.selected_type = obj_type

    def cycle_selected_type(self, step=1):
        keys = list(OBJECT_TYPES.keys())
        idx = keys.index(self.selected_type) if self.selected_type in keys else 0
        self.selected_type = keys[(idx + step) % len(keys)]
        return self.selected_type

    def get_selected_type(self):
        return self.selected_type

    def get_selected_label(self):
        return OBJECT_TYPES.get(self.selected_type, {}).get('label', self.selected_type)

    # ------------------------------------------------------------------
    # Mirroring (for stairs and similar non-symmetrical objects)
    def toggle_mirror_next(self):
        self.mirror_next = not self.mirror_next
        return self.mirror_next

    def set_mirror_next(self, mirrored):
        self.mirror_next = bool(mirrored)

    def get_mirror_next(self):
        return self.mirror_next

    def flip_top_object_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        stack = self.stacks.get(key)
        if not stack:
            return None
        top_level = max(stack.keys())
        obj_type, mirrored = stack[top_level]
        mirrored = not mirrored
        stack[top_level] = (obj_type, mirrored)
        return mirrored

    # ------------------------------------------------------------------
    def place_object_at(self, tile_x, tile_y, obj_type=None, mirrored=None, level=None):
        key = (int(tile_x), int(tile_y))
        stack = self.stacks.setdefault(key, {})
        if level is None:
            level = (max(stack.keys()) + 1) if stack else 0
        if level < 0 or level >= self.max_stack_height:
            if not stack:
                del self.stacks[key]
            return False
        if level in stack:
            if not stack:
                del self.stacks[key]
            return False
        final_mirrored = self.mirror_next if mirrored is None else bool(mirrored)
        stack[level] = (obj_type or self.selected_type, final_mirrored)
        return True

    def remove_top_object_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        stack = self.stacks.get(key)
        if not stack:
            return None
        top_level = max(stack.keys())
        removed_type, _removed_mirrored = stack.pop(top_level)
        self.fluid_fill.pop((key[0], key[1], top_level), None)
        if not stack:
            del self.stacks[key]
        return removed_type

    def remove_all_at(self, tile_x, tile_y):
        key = (int(tile_x), int(tile_y))
        for fkey in [k for k in self.fluid_fill if k[0] == key[0] and k[1] == key[1]]:
            del self.fluid_fill[fkey]
        return self.stacks.pop(key, None) is not None

    def has_object_at(self, tile_x, tile_y):
        return bool(self.stacks.get((int(tile_x), int(tile_y))))

    def get_stack_at(self, tile_x, tile_y):
        stack = self.stacks.get((int(tile_x), int(tile_y)), {})
        return [stack[level] for level in sorted(stack.keys())]

    def get_stack_with_levels(self, tile_x, tile_y):
        stack = self.stacks.get((int(tile_x), int(tile_y)), {})
        return [(level, *stack[level]) for level in sorted(stack.keys())]

    def get_top_object_type(self, tile_x, tile_y):
        stack = self.stacks.get((int(tile_x), int(tile_y)))
        if not stack:
            return None
        top_level = max(stack.keys())
        return stack[top_level][0]

    def get_object_at_level(self, tile_x, tile_y, level):
        stack = self.stacks.get((int(tile_x), int(tile_y)))
        if not stack:
            return None
        return stack.get(int(level))

    def get_stack_height(self, tile_x, tile_y):
        stack = self.stacks.get((int(tile_x), int(tile_y)))
        if not stack:
            return 0
        return max(stack.keys()) + 1

    def is_stack_full(self, tile_x, tile_y):
        return self.get_stack_height(tile_x, tile_y) >= self.max_stack_height

    def remove_all(self):
        self.stacks.clear()

    def get_objects(self):
        result = []
        for (tile_x, tile_y), stack in self.stacks.items():
            for level, (obj_type, mirrored) in stack.items():
                result.append(PlacedObject(tile_x, tile_y, obj_type, level, mirrored))
        return result

    # ------------------------------------------------------------------
    # Fluid fill level (water_cube / lava_cube)
    def get_fill(self, tile_x, tile_y, level):
        return self.fluid_fill.get((int(tile_x), int(tile_y), int(level)), 1.0)

    def set_fill(self, tile_x, tile_y, level, fraction):
        key = (int(tile_x), int(tile_y), int(level))
        fraction = max(0.0, min(1.0, fraction))
        if fraction >= 0.999:
            self.fluid_fill.pop(key, None)
        else:
            self.fluid_fill[key] = fraction

    def get_fluid_kind(self, obj_type):
        """'water', 'lava', or None — from OBJECT_TYPES[obj_type]['fluid']."""
        return OBJECT_TYPES.get(obj_type, {}).get('fluid')

    def count(self):

        return sum(len(stack) for stack in self.stacks.values())

    # ------------------------------------------------------------------

    def _find_file(self, obj_type):
        filename = OBJECT_TYPES.get(obj_type, {}).get('texture', obj_type)
        for directory in self.search_dirs:
            for ext in SUPPORTED_EXTENSIONS:
                path = os.path.join(directory, filename + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _load_raw(self, obj_type):
        if obj_type in self._raw_cache:
            cached = self._raw_cache[obj_type]
            return None if cached is _MISSING else cached

        path = self._find_file(obj_type)
        if path is None:
            searched = " or ".join(self.search_dirs)
            print(f"Object system: no texture found for '{obj_type}' "
                  f"(looked in {searched}) — drawing a placeholder box instead")
            self._raw_cache[obj_type] = _MISSING
            return None

        try:
            surface = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"Object system: failed to load texture for '{obj_type}' from {path}: {e} — "
                  f"drawing a placeholder box instead")
            self._raw_cache[obj_type] = _MISSING
            return None

        print(f"Object system: loaded texture for '{obj_type}' from {path}")
        self._raw_cache[obj_type] = surface
        return surface

    def _get_scaled(self, obj_type, width_px):
        raw = self._load_raw(obj_type)
        if raw is None:
            return None

        width_px = max(2, int(round(width_px)))
        key = (obj_type, width_px)
        cached = self._scaled_cache.get(key)
        if cached is not None:
            return cached

        raw_w, raw_h = raw.get_size()
        scale = width_px / raw_w
        height_px = max(2, int(round(raw_h * scale)))
        scaled = pygame.transform.smoothscale(raw, (width_px, height_px))

        if len(self._scaled_cache) > 300:
            self._scaled_cache.clear()
        self._scaled_cache[key] = scaled
        return scaled

    def _get_oriented(self, obj_type, width_px, mirrored):
        base = self._get_scaled(obj_type, width_px)
        if base is None:
            return None
        if not mirrored:
            return base

        mirror_mode = OBJECT_TYPES.get(obj_type, {}).get('mirror_mode', 'flip')
        key = (obj_type, width_px, 'mirrored', mirror_mode)
        cached = self._scaled_cache.get(key)
        if cached is not None:
            return cached

        if mirror_mode == 'rotate90':
            raw_w, raw_h = base.get_size()
            rotated = pygame.transform.rotate(base, 90)
            transformed = pygame.transform.smoothscale(rotated, (raw_w, raw_h))
        else:
            transformed = pygame.transform.flip(base, True, False)

        if len(self._scaled_cache) > 300:
            self._scaled_cache.clear()
        self._scaled_cache[key] = transformed
        return transformed

    def is_no_stack_type(self, obj_type):
        return bool(OBJECT_TYPES.get(obj_type, {}).get('no_stack', False))

    def _get_tinted(self, obj_type, width_px, mirrored, sprite, color, degrees=0):

        qcolor = (color[0] & ~7, color[1] & ~7, color[2] & ~7)
        key = (obj_type, width_px, mirrored, degrees, qcolor)
        cached = self._tint_cache.get(key)
        if cached is not None:
            return cached

        tinted = sprite.copy()
        tinted.fill((*qcolor, 255), special_flags=pygame.BLEND_RGBA_MULT)

        if len(self._tint_cache) > 500:
            self._tint_cache.clear()
        self._tint_cache[key] = tinted
        return tinted

    def get_preview_texture(self, obj_type, width_px):
        return self._get_scaled(obj_type, width_px)

    def get_type_ids(self):
        return list(OBJECT_TYPES.keys())

    def get_label_for(self, obj_type):
        return OBJECT_TYPES.get(obj_type, {}).get('label', obj_type)

    def get_light_config(self, obj_type):
        return OBJECT_TYPES.get(obj_type, {}).get('light')

    def reload(self):
        self._raw_cache.clear()
        self._scaled_cache.clear()
        self._tint_cache.clear()
        self._place_sound_cache.clear()
        self._place_sound_missing_logged.clear()

    # ------------------------------------------------------------------
    def _find_place_sound_path(self, obj_type):
        sound_key = OBJECT_TYPES.get(obj_type, {}).get('place_sound', obj_type)
        for directory in OBJECT_SOUND_SEARCH_DIRS:
            for ext in SUPPORTED_SOUND_EXTENSIONS:
                path = os.path.join(directory, sound_key + ext)
                if os.path.isfile(path):
                    return path
        return None

    def get_place_sound(self, obj_type):
        cached = self._place_sound_cache.get(obj_type)
        if cached is not None:
            return cached

        path = self._find_place_sound_path(obj_type)
        sound = None
        if path is not None:
            try:
                sound = pygame.mixer.Sound(path)
                print(f"Object system: loaded placement sound for '{obj_type}' from {path}")
            except Exception as e:
                print(f"Object system: failed to load placement sound for '{obj_type}' from {path}: {e}")

        if sound is None:
            material = OBJECT_TYPES.get(obj_type, {}).get('sound_material', 'wood')
            if obj_type not in self._place_sound_missing_logged:
                searched = " or ".join(OBJECT_SOUND_SEARCH_DIRS)
                sound_key = OBJECT_TYPES.get(obj_type, {}).get('place_sound', obj_type)
                print(f"Object system: no placement sound found for '{obj_type}' "
                      f"(looked for {sound_key}.wav/.mp3/.ogg in {searched}) — "
                      f"using a procedural '{material}' thud instead")
                self._place_sound_missing_logged.add(obj_type)
            try:
                seed = abs(hash(obj_type)) % 1_000_000
                signal = _make_place_sound_signal(material, seed)
                pcm = np.clip(signal, -1.0, 1.0)
                pcm = (pcm * 32767).astype(np.int16)
                stereo = np.column_stack([pcm, pcm])
                sound = pygame.sndarray.make_sound(stereo)
            except Exception as e:
                print(f"Object system: failed to generate placement sound for '{obj_type}': {e}")
                return None

        self._place_sound_cache[obj_type] = sound
        return sound

    def play_place_sound(self, obj_type, sound_system, volume=1.0):
        if sound_system is None or not getattr(sound_system, 'enabled', False):
            return
        sound = self.get_place_sound(obj_type)
        if sound is None:
            return
        if hasattr(sound_system, 'play_one_shot'):
            sound_system.play_one_shot(sound, volume=volume)

    def _draw_object(self, screen, obj_type, level, mirrored, screen_x, screen_y,
                      pixels_per_tile, half_tile, quarter_tile, light_color=None, rotation_fn=None, fill=1.0):
        type_info = OBJECT_TYPES.get(obj_type, {})
        width_scale = type_info.get('width_scale', 1.0)
        level_height_scale = type_info.get('level_height_scale', 0.5)
        width_px = max(2, int(round(pixels_per_tile * width_scale)))
        level_offset = level * pixels_per_tile * level_height_scale
        base_y = screen_y + quarter_tile - level_offset

        extra_flip = bool(rotation_fn(obj_type, mirrored)) if rotation_fn is not None else False
        effective_mirrored = mirrored != extra_flip

        sprite = self._get_oriented(obj_type, width_px, effective_mirrored)
        if sprite is not None:
            if light_color is not None:
                sprite = self._get_tinted(obj_type, width_px, effective_mirrored, sprite, light_color)
            if type_info.get('fluid') is not None:
                sprite = self._get_pulsed(sprite, screen_x, screen_y)
            if fill < 0.999:
                sprite = self._get_squashed(sprite, fill)
            rect = sprite.get_rect(midbottom=(screen_x, base_y))
            screen.blit(sprite, rect)
        else:
            self._draw_placeholder_cube(screen, screen_x, base_y, half_tile, quarter_tile,
                                         light_color, mirrored=effective_mirrored)

    PULSE_SPEED = 2.2       # radians/sec
    PULSE_AMPLITUDE = 0.07  # +/- brightness fraction

    def _get_pulsed(self, sprite, screen_x, screen_y):
        t = pygame.time.get_ticks() / 1000.0
        phase = screen_x * 0.013 + screen_y * 0.017
        brightness = 1.0 + self.PULSE_AMPLITUDE * math.sin(t * self.PULSE_SPEED + phase)
        level = max(0, min(255, int(round(255 * brightness))))
        pulsed = sprite.copy()
        pulsed.fill((level, level, level, 255), special_flags=pygame.BLEND_RGBA_MULT)
        return pulsed

    MIN_SQUASH_FRACTION = 0.4   # never squash a fluid sprite thinner than this — keeps a clearly visible chunk

    def _get_squashed(self, sprite, fill):
        fill = max(self.MIN_SQUASH_FRACTION, min(1.0, fill))
        w, h = sprite.get_size()
        new_h = max(1, int(round(h * fill)))
        if new_h >= h:
            return sprite

        return pygame.transform.scale(sprite, (w, new_h))

    # ------------------------------------------------------------------
    def render(self, screen, world_to_screen_fn, pixels_per_tile, chunk_bounds=None, light_fn=None, rotation_fn=None):
        if not self.stacks:
            return

        screen_w, screen_h = screen.get_size()
        half_tile = pixels_per_tile / 2
        quarter_tile = half_tile / 2

        objects_to_draw = self.get_objects()

        objects_to_draw.sort(key=lambda o: (o.tile_x + o.tile_y, o.level))

        for obj in objects_to_draw:
            if chunk_bounds is not None:
                min_x, min_y, max_x, max_y = chunk_bounds
                if not (min_x <= obj.tile_x < max_x and min_y <= obj.tile_y < max_y):
                    continue

            screen_x, screen_y = world_to_screen_fn(obj.tile_x, obj.tile_y)
            if not (-half_tile * 2 <= screen_x <= screen_w + half_tile * 2 and
                    -half_tile * 6 <= screen_y <= screen_h + half_tile * 4):
                continue

            light_color = light_fn(obj.tile_x, obj.tile_y) if light_fn is not None else None
            fill = self.fluid_fill.get((obj.tile_x, obj.tile_y, obj.level), 1.0)
            self._draw_object(screen, obj.obj_type, obj.level, obj.mirrored, screen_x, screen_y,
                               pixels_per_tile, half_tile, quarter_tile, light_color, rotation_fn=rotation_fn, fill=fill)

    def render_at_tile(self, screen, world_to_screen_fn, pixels_per_tile, tile_x, tile_y, light_fn=None, rotation_fn=None,
                        actor_depth_fn=None, actor_draw_fn=None, actor_state=None):
        key = (int(tile_x), int(tile_y))
        stack = self.stacks.get(key)

        if (stack and actor_depth_fn is not None and actor_draw_fn is not None and actor_state is not None
                and not actor_state.get('drawn') and actor_state.get('depth') is not None):
            if actor_depth_fn(tile_x, tile_y) > actor_state['depth']:
                actor_draw_fn()
                actor_state['drawn'] = True

        if not stack:
            return

        screen_w, screen_h = screen.get_size()
        screen_x, screen_y = world_to_screen_fn(tile_x, tile_y)
        half_tile = pixels_per_tile / 2
        quarter_tile = half_tile / 2

        if not (-half_tile * 2 <= screen_x <= screen_w + half_tile * 2 and
                -half_tile * 6 <= screen_y <= screen_h + half_tile * 4):
            return

        light_color = light_fn(tile_x, tile_y) if light_fn is not None else None

        for level in sorted(stack.keys()):
            obj_type, mirrored = stack[level]
            fill = self.fluid_fill.get((int(tile_x), int(tile_y), level), 1.0)
            self._draw_object(screen, obj_type, level, mirrored, screen_x, screen_y,
                               pixels_per_tile, half_tile, quarter_tile, light_color, rotation_fn=rotation_fn, fill=fill)

    @staticmethod
    def _draw_placeholder_cube(screen, screen_x, base_y, half_tile, quarter_tile, light_color=None, mirrored=False):
        cube_height = quarter_tile * 3.2
        top_y = base_y - cube_height

        color_top = (191, 143, 90)
        color_left = (140, 100, 62)
        color_right = (166, 120, 74)

        if mirrored:
            color_left, color_right = color_right, color_left

        if light_color is not None:
            def _tint(c):
                return tuple(max(0, min(255, int(c[i] * light_color[i] / 255.0))) for i in range(3))
            color_top = _tint(color_top)
            color_left = _tint(color_left)
            color_right = _tint(color_right)

        top_points = [
            (screen_x, top_y - quarter_tile),
            (screen_x + half_tile, top_y),
            (screen_x, top_y + quarter_tile),
            (screen_x - half_tile, top_y),
        ]
        left_points = [
            (screen_x - half_tile, top_y),
            (screen_x, top_y + quarter_tile),
            (screen_x, base_y + quarter_tile),
            (screen_x - half_tile, base_y),
        ]
        right_points = [
            (screen_x + half_tile, top_y),
            (screen_x, top_y + quarter_tile),
            (screen_x, base_y + quarter_tile),
            (screen_x + half_tile, base_y),
        ]

        pygame.draw.polygon(screen, color_left, left_points)
        pygame.draw.polygon(screen, color_right, right_points)
        pygame.draw.polygon(screen, color_top, top_points)
        pygame.draw.polygon(screen, (60, 45, 30), top_points, 1)

    def render_preview(self, screen, world_to_screen_fn, pixels_per_tile, tile_x, tile_y, alpha=140, blocked=False, rotation_fn=None):
        if self.is_stack_full(tile_x, tile_y):
            return

        obj_type = self.selected_type
        mirrored = self.mirror_next
        level = self.get_stack_height(tile_x, tile_y)

        screen_x, screen_y = world_to_screen_fn(tile_x, tile_y)
        half_tile = pixels_per_tile / 2
        quarter_tile = half_tile / 2

        diamond_points = [
            (screen_x, screen_y - quarter_tile),
            (screen_x + half_tile, screen_y),
            (screen_x, screen_y + quarter_tile),
            (screen_x - half_tile, screen_y),
        ]
        outline_color = (225, 60, 60) if blocked else (255, 255, 255)
        pygame.draw.polygon(screen, outline_color, diamond_points, 2)

        if blocked:
            return

        type_info = OBJECT_TYPES.get(obj_type, {})
        width_scale = type_info.get('width_scale', 1.0)
        level_height_scale = type_info.get('level_height_scale', 0.5)
        width_px = max(2, int(round(pixels_per_tile * width_scale)))
        level_offset = level * pixels_per_tile * level_height_scale
        base_y = screen_y + quarter_tile - level_offset

        extra_flip = bool(rotation_fn(obj_type, mirrored)) if rotation_fn is not None else False
        effective_mirrored = mirrored != extra_flip

        sprite = self._get_oriented(obj_type, width_px, effective_mirrored)

        if sprite is not None:
            preview = sprite.copy()
            preview.set_alpha(alpha)
            rect = preview.get_rect(midbottom=(screen_x, base_y))
            screen.blit(preview, rect)
        else:
            ghost = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
            self._draw_placeholder_cube(ghost, screen_x, base_y, half_tile, quarter_tile, mirrored=effective_mirrored)
            ghost.set_alpha(alpha)
            screen.blit(ghost, (0, 0))

    # ------------------------------------------------------------------
    def render_placement_markers(self, screen, world_to_screen_fn, pixels_per_tile):
        if not self.stacks:
            return
        screen_w, screen_h = screen.get_size()
        for (tile_x, tile_y) in self.stacks:
            screen_x, screen_y = world_to_screen_fn(tile_x, tile_y)
            if -20 <= screen_x <= screen_w + 20 and -20 <= screen_y <= screen_h + 20:
                r = max(2, int(pixels_per_tile * 0.06))
                pygame.draw.circle(screen, (90, 200, 255), (int(screen_x), int(screen_y)), r, 1)

    def get_status_text(self):
        mirror_flag = " [mirrored]" if self.mirror_next else ""
        return f"{self.get_selected_label()}{mirror_flag} ({self.count()} placed, max {self.max_stack_height}/tile)"
