import os
import math
import numpy as np
import pygame


BIOME_FOREST = 'forest'
BIOME_PLAINS = 'plains'
BIOME_DESERT = 'desert'
BIOME_WATER = 'water'
BIOME_MOUNTAINS = 'mountains'
BIOME_SWAMP = 'swamp'

WEATHER_RAIN = 'rain'
WEATHER_SNOW = 'snow'
WEATHER_SAND = 'sand'

BIOME_FILENAMES = {
    BIOME_FOREST: 'forest',
    BIOME_PLAINS: 'plains',
    BIOME_DESERT: 'desert',
    BIOME_WATER: 'water',
    BIOME_MOUNTAINS: 'mountains',
    BIOME_SWAMP: 'swamp',
}

SUPPORTED_EXTENSIONS = ('.wav', '.mp3', '.ogg')

BIOME_SOUND_SEARCH_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sound', 'bioms'),
    '/sound/bioms',
]


def _lowpass(signal, window):
    if window <= 1:
        return signal
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(signal, kernel, mode='same').astype(np.float32)


def _make_seamless(signal, overlap):
    n = len(signal)
    overlap = min(overlap, n // 4)
    if overlap <= 0:
        return signal
    fade = np.linspace(0, 1, overlap, dtype=np.float32)
    out = signal.copy()
    out[:overlap] = signal[:overlap] * fade + signal[-overlap:] * (1 - fade)
    return out


def _to_stereo_int16(mono):
    mono = np.clip(mono, -1.0, 1.0)
    pcm = (mono * 32767).astype(np.int16)
    return np.column_stack([pcm, pcm])


class SoundSystem:
    SAMPLE_RATE = 22050
    FADE_SPEED = 1.0 / 1.5   
    WEATHER_FADE_SPEED = 1.0 / 1.0


    EFFECT_CHANNEL_COUNT = 8

    def __init__(self, master_volume=0.6, seed=0, biome_sound_dirs=None):
        self.master_volume = master_volume
        self.enabled = False
        self.rng = np.random.RandomState(seed)

        self.biome_sound_dirs = biome_sound_dirs or BIOME_SOUND_SEARCH_DIRS

        self._biome_channels = {}
        self._biome_volume = {}
        self._biome_target = {}

        self._weather_channels = {}
        self._weather_volume = {}
        self._weather_target_intensity = {}

        self.current_biome = None
        self.current_weather = None
        self._weather_intensity = 0.0


        self._next_channel_index = 0
        self._effect_channels = []
        self._effect_channel_cursor = 0
        self._dedicated_channels = {}   # key -> Channel


        self._active_chunk_bounds = None

        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(frequency=self.SAMPLE_RATE, size=-16, channels=2)

            pygame.mixer.set_num_channels(max(32, pygame.mixer.get_num_channels()))
            self._build_all_sounds()
            self.enabled = True
        except Exception as e:
            print(f"Sound is unavailable. ({e}) — The game will continue running without sound.")
            self.enabled = False

    def _reserve_channel(self):
        channel = pygame.mixer.Channel(self._next_channel_index)
        self._next_channel_index += 1
        return channel


    def _white_noise(self, seconds, seed_offset=0):
        rng = np.random.RandomState(int(self.rng.randint(0, 1_000_000)) + seed_offset)
        n = int(self.SAMPLE_RATE * seconds)
        return rng.uniform(-1.0, 1.0, n).astype(np.float32)

    def _make_wind(self, seconds, cutoff, amplitude, gust_period=None, gust_depth=0.3, seed_offset=0):
        noise = self._white_noise(seconds, seed_offset)
        noise = _lowpass(noise, cutoff)
        noise = noise / (np.abs(noise).max() + 1e-6)
        if gust_period:
            t = np.arange(len(noise)) / self.SAMPLE_RATE
            gust = 1.0 - gust_depth + gust_depth * (0.5 + 0.5 * np.sin(2 * math.pi * t / gust_period))
            noise = noise * gust
        return noise * amplitude

    def _add_chirps(self, base, count, freq_range, seed_offset=0):
        rng = np.random.RandomState(int(self.rng.randint(0, 1_000_000)) + seed_offset)
        n = len(base)
        out = base.copy()
        for _ in range(count):
            start = rng.randint(0, max(1, n - self.SAMPLE_RATE // 4))
            dur = rng.uniform(0.08, 0.22)
            length = int(self.SAMPLE_RATE * dur)
            length = min(length, n - start)
            if length <= 0:
                continue
            freq = rng.uniform(*freq_range)
            t = np.arange(length) / self.SAMPLE_RATE
            envelope = np.sin(math.pi * t / dur) ** 2  # smooth fade-in/fade-out
            chirp = np.sin(2 * math.pi * freq * t) * envelope * 0.35
            out[start:start + length] += chirp.astype(np.float32)
        return out

    def _make_waves(self, seconds, seed_offset=0):
        noise = self._white_noise(seconds, seed_offset)
        noise = _lowpass(noise, 25)
        noise = noise / (np.abs(noise).max() + 1e-6)
        t = np.arange(len(noise)) / self.SAMPLE_RATE
        wave = 0.55 + 0.45 * np.sin(2 * math.pi * t / 4.5) ** 2
        return noise * wave * 0.5

    def _find_biome_file(self, category):
        filename = BIOME_FILENAMES[category]
        for directory in self.biome_sound_dirs:
            for ext in SUPPORTED_EXTENSIONS:
                path = os.path.join(directory, filename + ext)
                if os.path.isfile(path):
                    return path
        return None

    def _fallback_biome_signal(self, category, seconds):
        generators = {
            BIOME_FOREST: lambda: self._add_chirps(
                self._make_wind(seconds, cutoff=35, amplitude=0.18, gust_period=7.0, seed_offset=1),
                count=10, freq_range=(1200, 2600), seed_offset=11),
            BIOME_PLAINS: lambda: self._make_wind(seconds, cutoff=15, amplitude=0.22, gust_period=5.0, seed_offset=2),
            BIOME_DESERT: lambda: self._make_wind(seconds, cutoff=8, amplitude=0.20, gust_period=3.0, gust_depth=0.5, seed_offset=3),
            BIOME_WATER: lambda: self._make_waves(seconds, seed_offset=4),
            BIOME_MOUNTAINS: lambda: self._make_wind(seconds, cutoff=45, amplitude=0.16, gust_period=9.0, gust_depth=0.4, seed_offset=5),
            BIOME_SWAMP: lambda: self._add_chirps(
                self._make_wind(seconds, cutoff=50, amplitude=0.12, seed_offset=6),
                count=6, freq_range=(150, 320), seed_offset=16),
        }
        return generators[category]()

    def _build_all_sounds(self):
        dur = 6.0  # duration of the substitute procedural sound and weather sounds
        overlap = int(self.SAMPLE_RATE * 0.5)


        for name in BIOME_FILENAMES:
            path = self._find_biome_file(name)
            sound = None
            if path is not None:
                try:
                    sound = pygame.mixer.Sound(path)
                except Exception as e:
                    print(f"Failed to load biome sound '{name}' из {path}: {e}")
            
            if sound is None:
                searched = " or ".join(self.biome_sound_dirs)
                print(f"Biome Sound File '{name}' not found "
                      f"(was looking for{BIOME_FILENAMES[name]}.wav/.mp3/.ogg в {searched}) — "
                      f"I'm using a fallback procedural sound instead of it.")
                signal = _make_seamless(self._fallback_biome_signal(name, dur), overlap)
                sound = pygame.sndarray.make_sound(_to_stereo_int16(signal))
            
            channel = self._reserve_channel()
            channel.play(sound, loops=-1)
            channel.set_volume(0.0)
            self._biome_channels[name] = channel
            self._biome_volume[name] = 0.0
            self._biome_target[name] = 0.0

        weather_generators = {
            WEATHER_RAIN: lambda: _lowpass(self._white_noise(dur, seed_offset=21), 4) * 0.5,
            WEATHER_SNOW: lambda: _lowpass(self._white_noise(dur, seed_offset=22), 60) * 0.25,
            WEATHER_SAND: lambda: self._make_wind(dur, cutoff=6, amplitude=0.45, gust_period=2.0, gust_depth=0.6, seed_offset=23),
        }
        for name, gen in weather_generators.items():
            signal = _make_seamless(gen(), overlap)
            sound = pygame.sndarray.make_sound(_to_stereo_int16(signal))
            channel = self._reserve_channel()
            channel.play(sound, loops=-1)
            channel.set_volume(0.0)
            self._weather_channels[name] = channel
            self._weather_volume[name] = 0.0
            self._weather_target_intensity[name] = 0.0

        self._effect_channels = [self._reserve_channel() for _ in range(self.EFFECT_CHANNEL_COUNT)]
        self._effect_channel_cursor = 0


    def set_dominant_biome(self, category):
        if not self.enabled:
            return
        self.current_biome = category
        for name in self._biome_target:
            self._biome_target[name] = 1.0 if name == category else 0.0

    def set_weather(self, kind, intensity):
        if not self.enabled:
            return
        self.current_weather = kind
        intensity = max(0.0, min(1.0, intensity))
        self._weather_intensity = intensity
        for name in self._weather_target_intensity:
            self._weather_target_intensity[name] = intensity if name == kind else 0.0

    def set_master_volume(self, volume):
        self.master_volume = max(0.0, min(1.0, volume))

    def get_master_volume(self):
        return self.master_volume

    # ------------------------------------------------------------------
    def play_one_shot(self, sound, volume=1.0):
        if not self.enabled or sound is None or not self._effect_channels:
            return None
        channel = self._effect_channels[self._effect_channel_cursor]
        self._effect_channel_cursor = (self._effect_channel_cursor + 1) % len(self._effect_channels)
        channel.set_volume(max(0.0, min(1.0, volume)) * self.master_volume)
        channel.play(sound)
        return channel

    def get_dedicated_channel(self, key):
        if not self.enabled:
            return None
        channel = self._dedicated_channels.get(key)
        if channel is None:
            channel = self._reserve_channel()
            self._dedicated_channels[key] = channel
        return channel

    def set_active_chunk_bounds(self, chunk_bounds):
        self._active_chunk_bounds = chunk_bounds

    def get_active_chunk_bounds(self):
        return self._active_chunk_bounds

    def is_position_audible(self, tile_x, tile_y, margin=0):
        if self._active_chunk_bounds is None:
            return True
        min_x, min_y, max_x, max_y = self._active_chunk_bounds
        return (min_x - margin <= tile_x < max_x + margin and
                min_y - margin <= tile_y < max_y + margin)

    def update(self, dt_seconds):
        if not self.enabled:
            return

        for name, channel in self._biome_channels.items():
            target = self._biome_target[name]
            vol = self._biome_volume[name]
            if vol < target:
                vol = min(target, vol + self.FADE_SPEED * dt_seconds)
            elif vol > target:
                vol = max(target, vol - self.FADE_SPEED * dt_seconds)
            self._biome_volume[name] = vol
            channel.set_volume(vol * self.master_volume)

        for name, channel in self._weather_channels.items():
            target = self._weather_target_intensity[name]
            vol = self._weather_volume[name]
            if vol < target:
                vol = min(target, vol + self.WEATHER_FADE_SPEED * dt_seconds)
            elif vol > target:
                vol = max(target, vol - self.WEATHER_FADE_SPEED * dt_seconds)
            self._weather_volume[name] = vol
            channel.set_volume(vol * self.master_volume)

    def get_status_text(self):
        if not self.enabled:
            return "sound not found"
        parts = [self.current_biome or "—"]
        if self.current_weather and self._weather_intensity > 0.02:
            parts.append(f"{self.current_weather} {int(self._weather_intensity * 100)}%")
        return " + ".join(parts)

    def stop_all(self):
        if not self.enabled:
            return
        for channel in self._biome_channels.values():
            channel.stop()
        for channel in self._weather_channels.values():
            channel.stop()
        for channel in self._effect_channels:
            channel.stop()
