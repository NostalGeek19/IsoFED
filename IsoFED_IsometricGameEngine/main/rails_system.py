import os
import math
import numpy as np
import pygame


RAIL_TYPE = 'rail'
CART_TYPE = 'cart'

AXIS_EW = 'ew'  
AXIS_NS = 'ns'   

DEFAULT_CART_SPEED = 2.2  

CART_SOUND_SAMPLE_RATE = 22050
CART_SOUND_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sound', 'rails'),
    '/sound/rails',
]
SUPPORTED_SOUND_EXTENSIONS = ('.wav', '.mp3', '.ogg')
MOVE_SOUND_CHANNEL_KEY = 'cart_move'
MOVE_SOUND_FADE_SPEED = 1.0 / 0.25   
MOVE_SOUND_VOLUME = 0.6


# ----------------------------------------------------------------------
def get_rail_axis(mirrored):
    return AXIS_NS if mirrored else AXIS_EW


def needs_camera_flip(obj_type, camera_rotation_steps):
    if obj_type not in (RAIL_TYPE, CART_TYPE):
        return False
    return int(camera_rotation_steps) % 2 == 1


# ----------------------------------------------------------------------
def can_place_rail(objects, tile_x, tile_y, is_blocked_fn=None):
    if objects.has_object_at(tile_x, tile_y):
        return False
    if is_blocked_fn is not None and is_blocked_fn(tile_x, tile_y):
        return False
    return True


def can_place_cart(objects, tile_x, tile_y):
    return (objects.get_stack_height(tile_x, tile_y) == 1 and
            objects.get_top_object_type(tile_x, tile_y) == RAIL_TYPE)


def get_rail_mirrored_at(objects, tile_x, tile_y):
    for level, obj_type, mirrored in objects.get_stack_with_levels(tile_x, tile_y):
        if obj_type == RAIL_TYPE:
            return mirrored
    return None


_NEIGHBOR_OFFSETS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def find_connected_rail_directions(objects, tile_x, tile_y):
    result = []
    for dx, dy in _NEIGHBOR_OFFSETS:
        if can_place_cart(objects, tile_x + dx, tile_y + dy):
            result.append((dx, dy))
    return result


def choose_direction(objects, tile_x, tile_y, want_positive, avoid=None):
    candidates = find_connected_rail_directions(objects, tile_x, tile_y)
    if not candidates:
        return None
    if avoid is not None and len(candidates) > 1:
        filtered = [c for c in candidates if c != avoid]
        if filtered:
            candidates = filtered

    if want_positive:
        preferred = [c for c in candidates if c[0] > 0 or (c[0] == 0 and c[1] > 0)]
    else:
        preferred = [c for c in candidates if c[0] < 0 or (c[0] == 0 and c[1] < 0)]
    return preferred[0] if preferred else candidates[0]


# ----------------------------------------------------------------------
def _make_seamless(signal, overlap):
    n = len(signal)
    overlap = min(overlap, n // 4)
    if overlap <= 0:
        return signal
    fade = np.linspace(0, 1, overlap, dtype=np.float32)
    out = signal.copy()
    out[:overlap] = signal[:overlap] * fade + signal[-overlap:] * (1 - fade)
    return out


def _make_cart_roll_signal(seed):
    rng = np.random.RandomState(seed)
    duration = 1.6
    n = int(CART_SOUND_SAMPLE_RATE * duration)
    t = np.arange(n) / CART_SOUND_SAMPLE_RATE

    noise = rng.uniform(-1.0, 1.0, n).astype(np.float32)
    kernel = np.ones(28, dtype=np.float32) / 28
    rumble = np.convolve(noise, kernel, mode='same').astype(np.float32)
    rumble = rumble / (np.abs(rumble).max() + 1e-6) * 0.35

    clack = np.zeros(n, dtype=np.float32)
    clack_period = 0.4
    clack_len = int(CART_SOUND_SAMPLE_RATE * 0.05)
    pos = 0
    while pos < n:
        end = min(n, pos + clack_len)
        seg_len = end - pos
        seg_t = np.arange(seg_len) / CART_SOUND_SAMPLE_RATE
        envelope = np.exp(-seg_t * 40.0).astype(np.float32)
        seg_noise = rng.uniform(-1.0, 1.0, seg_len).astype(np.float32)
        clack[pos:end] += seg_noise * envelope * 0.55
        pos += int(CART_SOUND_SAMPLE_RATE * clack_period)

    signal = rumble + clack
    peak = float(np.abs(signal).max()) + 1e-6
    signal = (signal / peak) * 0.8
    return signal.astype(np.float32)


# ----------------------------------------------------------------------
class RailCart:
    __slots__ = ('tile_x', 'tile_y', 'move_offset', 'moving', 'progress',
                 'from_tile', 'to_tile')

    def __init__(self, tile_x, tile_y):
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.move_offset = None   # (dx, dy)
        self.moving = False
        self.progress = 0.0      
        self.from_tile = (tile_x, tile_y)
        self.to_tile = (tile_x, tile_y)

    def get_render_position(self):
        if self.progress <= 0.0:
            return float(self.tile_x), float(self.tile_y)
        fx, fy = self.from_tile
        tx, ty = self.to_tile
        t = min(1.0, self.progress)
        return fx + (tx - fx) * t, fy + (ty - fy) * t


class RailsSystem:
    def __init__(self, move_speed=DEFAULT_CART_SPEED, sound_system=None, sound_dirs=None):
        self.move_speed = move_speed
        self.cart = None   # RailCart

        self.sound_system = sound_system
        self.sound_dirs = sound_dirs or CART_SOUND_SEARCH_DIRS
        self._move_sound = None       # pygame.mixer.Sound
        self._move_sound_missing_logged = False
        self._move_channel_volume = 0.0   

    # ------------------------------------------------------------------
    def _find_move_sound_path(self):
        for directory in self.sound_dirs:
            for ext in SUPPORTED_SOUND_EXTENSIONS:
                path = os.path.join(directory, 'cart_move' + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _get_move_sound(self):
        if self._move_sound is not None:
            return self._move_sound
        if pygame.mixer.get_init() is None:
            return None

        path = self._find_move_sound_path()
        if path is not None:
            try:
                self._move_sound = pygame.mixer.Sound(path)
                print(f"Rails system: loaded cart movement sound from {path}")
                return self._move_sound
            except Exception as e:
                print(f"Rails system: failed to load cart movement sound from {path}: {e}")

        if not self._move_sound_missing_logged:
            searched = " or ".join(self.sound_dirs)
            print(f"Rails system: no cart movement sound found (looked for "
                  f"cart_move.wav/.mp3/.ogg in {searched}) — using a procedural rattle instead")
            self._move_sound_missing_logged = True

        try:
            signal = _make_seamless(_make_cart_roll_signal(seed=7),
                                     int(CART_SOUND_SAMPLE_RATE * 0.3))
            pcm = np.clip(signal, -1.0, 1.0)
            pcm = (pcm * 32767).astype(np.int16)
            stereo = np.column_stack([pcm, pcm])
            self._move_sound = pygame.sndarray.make_sound(stereo)
        except Exception as e:
            print(f"Rails system: failed to generate cart movement sound: {e}")
            self._move_sound = None
        return self._move_sound

    def _update_move_sound(self, dt):
        if self.sound_system is None or not getattr(self.sound_system, 'enabled', False):
            return

        channel = self.sound_system.get_dedicated_channel(MOVE_SOUND_CHANNEL_KEY)
        if channel is None:
            return

        target = MOVE_SOUND_VOLUME if (self.cart is not None and self.cart.moving) else 0.0

        if target > 0.0 and not channel.get_busy():
            sound = self._get_move_sound()
            if sound is not None:
                channel.play(sound, loops=-1)

        vol = self._move_channel_volume
        if vol < target:
            vol = min(target, vol + MOVE_SOUND_FADE_SPEED * dt)
        elif vol > target:
            vol = max(target, vol - MOVE_SOUND_FADE_SPEED * dt)
        self._move_channel_volume = vol

        master = getattr(self.sound_system, 'master_volume', 1.0)
        channel.set_volume(vol * master)

        if vol <= 0.0 and channel.get_busy():
            channel.stop()

    # ------------------------------------------------------------------
    def is_locked(self):
        return self.cart is not None

    def locked_tile(self):
        return (self.cart.tile_x, self.cart.tile_y) if self.cart is not None else None

    def toggle_lock(self, objects, tile_x, tile_y):
        tile_x, tile_y = int(tile_x), int(tile_y)

        if self.cart is not None:
            self.cart = None
            return False

        if objects.get_top_object_type(tile_x, tile_y) != CART_TYPE:
            return False

        self.cart = RailCart(tile_x, tile_y)
        return True

    def set_direction(self, direction, objects):
        if self.cart is None:
            return None
        avoid = None
        if self.cart.move_offset is not None:
            avoid = (-self.cart.move_offset[0], -self.cart.move_offset[1])
        chosen = choose_direction(objects, self.cart.tile_x, self.cart.tile_y,
                                   want_positive=(direction > 0), avoid=avoid)
        if chosen is None:
            return None
        self.cart.move_offset = chosen
        self.cart.moving = True
        return chosen

    def stop(self):
        if self.cart is not None:
            self.cart.moving = False
            self.cart.move_offset = None

    def get_camera_target_tile(self):
        if self.cart is None:
            return None
        return self.cart.get_render_position()

    # ------------------------------------------------------------------
    def update(self, dt, objects):
        self._update_move_sound(dt)

        if self.cart is None:
            return

        cart = self.cart

        if cart.progress <= 0.0:
            if objects.get_top_object_type(cart.tile_x, cart.tile_y) != CART_TYPE:
                self.cart = None
                return
        else:
            from_x, from_y = cart.from_tile
            if objects.get_top_object_type(from_x, from_y) != CART_TYPE:
                self.cart = None
                return
            to_x, to_y = cart.to_tile
            if not can_place_cart(objects, to_x, to_y):
                cart.moving = False
                cart.progress = 0.0
                return

        if not cart.moving:
            return

        if cart.progress <= 0.0:
            if cart.move_offset is None:
                cart.moving = False
                return

            dx, dy = cart.move_offset
            next_tile = (cart.tile_x + dx, cart.tile_y + dy)

            if not can_place_cart(objects, next_tile[0], next_tile[1]):
                cart.moving = False
                return

            cart.from_tile = (cart.tile_x, cart.tile_y)
            cart.to_tile = next_tile

        cart.progress += self.move_speed * dt
        if cart.progress >= 1.0:
            self._finish_move(objects)

    def _finish_move(self, objects):
        cart = self.cart
        from_x, from_y = cart.from_tile
        to_x, to_y = cart.to_tile

        rail_mirrored = get_rail_mirrored_at(objects, to_x, to_y)
        objects.remove_top_object_at(from_x, from_y)
        objects.place_object_at(to_x, to_y, CART_TYPE, mirrored=rail_mirrored)

        cart.tile_x, cart.tile_y = to_x, to_y
        cart.progress = 0.0
