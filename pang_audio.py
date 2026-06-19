"""Game Boy style chiptune engine for Pang Deluxe (v2 — richer synth & comp).

Channels (à la Game Boy):
  pulse1 / pulse2 — squares with selectable duty, PWM, pitch envelope, vibrato
  wave            — 4-bit programmable waveform (bass / pads)
  noise           — LFSR pseudo-random
  drum            — composite voice (pitched body + LFSR noise) for kick/snare/hat

The whole mix runs through a tap-delay echo + soft FIR low-pass + soft
limiter to fake the Game Boy speaker character with a touch of polish.

Music is sequenced from compact pattern data on a worker thread. Songs can
have multiple sections by chaining patterns. SFX are one-shot voice trees.

Drop-in safe: imports `numpy` and `sounddevice`; if either is missing or no
audio device is available, `make_audio()` returns a no-op stub.
"""

from __future__ import annotations

import heapq
import threading
import time

try:
    import numpy as np
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except Exception:
    AUDIO_AVAILABLE = False


SAMPLE_RATE = 44100
BUFFER_SIZE = 512
MAX_VOICES  = 40

# --- Notes ------------------------------------------------------------------
NOTE_OFFSETS = {
    'C': -9, 'C#': -8, 'Db': -8, 'D': -7, 'D#': -6, 'Eb': -6, 'E': -5,
    'F': -4, 'F#': -3, 'Gb': -3, 'G': -2, 'G#': -1, 'Ab': -1,
    'A': 0,  'A#': 1,  'Bb': 1,  'B': 2,
}


def note_to_freq(name):
    if not isinstance(name, str) or name in ('rest', '-'):
        return 0.0
    if len(name) >= 3 and name[1] in '#b':
        n, octave = name[:2], int(name[2:])
    else:
        n, octave = name[0], int(name[1:])
    semitones = NOTE_OFFSETS[n] + (octave - 4) * 12
    return 440.0 * (2.0 ** (semitones / 12.0))


# --- Audio-only globals -----------------------------------------------------
if AUDIO_AVAILABLE:
    def _make_wave(name, n=32):
        if name == 'tri':
            half = np.linspace(-1, 1, n // 2, endpoint=False)
            return np.concatenate([half, half[::-1]])
        if name == 'saw':
            return np.linspace(-1, 1, n, endpoint=False)
        if name == 'sine':
            return np.sin(2 * np.pi * np.arange(n) / n)
        if name == 'pulse25':
            return np.array([1.0] * 8 + [-1.0] * 24)
        if name == 'organ':
            t = np.arange(n) / n
            return (np.sin(2 * np.pi * t) * 0.6
                    + np.sin(4 * np.pi * t) * 0.3
                    + np.sin(8 * np.pi * t) * 0.1)
        return np.linspace(-1, 1, n, endpoint=False)

    def _quantize_4bit(w):
        return np.round((w + 1) * 7.5) / 7.5 - 1.0

    WAVE_TABLES = {
        n: _quantize_4bit(_make_wave(n)).astype(np.float32)
        for n in ('tri', 'saw', 'sine', 'pulse25', 'organ')
    }

    def _gen_lfsr(n_steps, mode=15, seed=0x7FFF):
        out = np.empty(n_steps, dtype=np.float32)
        lfsr = seed
        for i in range(n_steps):
            out[i] = -1.0 if (lfsr & 1) else 1.0
            bit = (lfsr ^ (lfsr >> 1)) & 1
            lfsr = (lfsr >> 1) | (bit << 14)
            if mode == 7:
                lfsr = (lfsr & ~(1 << 6)) | (bit << 6)
        return out

    LFSR_15 = _gen_lfsr(32768, mode=15)
    LFSR_7  = _gen_lfsr(127,   mode=7)

    # FIR low-pass (12 taps) — softens harsh squares like the GB speaker
    _kw = np.array([0.62 ** i for i in range(12)], dtype=np.float32)
    LPF_KERNEL = (_kw / _kw.sum()).astype(np.float32)


# --- ADSR envelope ----------------------------------------------------------
class Envelope:
    __slots__ = ('a', 'd', 's', 'r', 'duration', 'hold', '_total')

    def __init__(self, attack=0.01, decay=0.05, sustain=0.6, release=0.05, duration=0.5):
        self.a = max(attack, 1e-5)
        self.d = max(decay, 1e-5)
        self.s = float(sustain)
        self.r = max(release, 1e-5)
        self.duration = max(duration, self.r)
        self.hold = max(0.0, self.duration - self.r)
        self._total = self.hold + self.r

    def total(self):
        return self._total

    def render(self, n, sr, t_start):
        t = t_start + np.arange(n, dtype=np.float32) / sr
        env = np.zeros(n, dtype=np.float32)
        a, d, s, r, hold = self.a, self.d, self.s, self.r, self.hold

        m = t < a
        if m.any():
            env[m] = t[m] / a
        m = (t >= a) & (t < a + d)
        if m.any():
            env[m] = 1.0 - (1.0 - s) * (t[m] - a) / d
        m = (t >= a + d) & (t < hold)
        if m.any():
            env[m] = s
        m = (t >= hold) & (t < hold + r)
        if m.any():
            if hold < a:
                start_v = hold / a
            elif hold < a + d:
                start_v = 1.0 - (1.0 - s) * (hold - a) / d
            else:
                start_v = s
            env[m] = start_v * (1.0 - (t[m] - hold) / r)
        return env


# --- Voices -----------------------------------------------------------------
class _Voice:
    __slots__ = ('t', 'done', 'bus')
    def __init__(self):
        self.t = 0.0
        self.done = False
        self.bus = 'main'   # 'main' or 'wet' (echo send)

    def render(self, buf, wet, sr):
        raise NotImplementedError


class PulseVoice(_Voice):
    """Square wave with duty cycle, optional sweep, vibrato, PWM, pitch env."""
    __slots__ = ('freq', 'duty', 'env', 'vol', 'sweep', 'vibrato',
                 'pwm', 'pitch_env', 'phase')

    def __init__(self, freq, duty, env, vol=0.4,
                 sweep=None, vibrato=None, pwm=None, pitch_env=None, bus='main'):
        super().__init__()
        self.bus = bus
        self.freq = freq
        self.duty = duty
        self.env = env
        self.vol = vol
        self.sweep = sweep            # (target_freq, sweep_seconds)
        self.vibrato = vibrato        # (rate_hz, depth_cents)
        self.pwm = pwm                # (rate_hz, depth_0_to_0.5)
        self.pitch_env = pitch_env    # (delta_semitones, decay_seconds) at note start
        self.phase = 0.0

    def render(self, buf, wet, sr):
        n = len(buf)
        t_arr = self.t + np.arange(n, dtype=np.float32) / sr

        if self.sweep:
            target_f, sweep_t = self.sweep
            progress = np.clip(t_arr / max(sweep_t, 1e-5), 0.0, 1.0)
            f = self.freq + (target_f - self.freq) * progress
        else:
            f = np.full(n, self.freq, dtype=np.float32)
        if self.pitch_env:
            delta_st, decay_t = self.pitch_env
            decay = np.exp(-t_arr / max(decay_t, 1e-5))
            f = f * (2.0 ** (delta_st / 12.0 * decay))
        if self.vibrato:
            rate, cents = self.vibrato
            ramp = np.clip(t_arr / 0.15, 0, 1)  # vibrato fades in over 150ms
            f = f * (2.0 ** (cents / 1200.0 * ramp * np.sin(2.0 * np.pi * rate * t_arr)))

        dphase = f / sr
        phases = (self.phase + np.cumsum(dphase)) % 1.0
        self.phase = float(phases[-1])

        if self.pwm:
            rate, depth = self.pwm
            duty = self.duty + depth * np.sin(2.0 * np.pi * rate * t_arr)
            duty = np.clip(duty, 0.05, 0.95)
            samples = np.where(phases < duty, 1.0, -1.0).astype(np.float32)
        else:
            samples = np.where(phases < self.duty, 1.0, -1.0).astype(np.float32)

        env = self.env.render(n, sr, self.t)
        out = samples * env * self.vol
        if self.bus == 'wet':
            wet += out
        else:
            buf += out

        self.t += n / sr
        if self.t >= self.env.total():
            self.done = True


class WaveVoice(_Voice):
    __slots__ = ('freq', 'env', 'vol', 'wave', 'phase', 'vibrato')

    def __init__(self, freq, env, vol=0.4, wave='tri', vibrato=None, bus='main'):
        super().__init__()
        self.bus = bus
        self.freq = freq
        self.env = env
        self.vol = vol
        self.wave = WAVE_TABLES[wave]
        self.phase = 0.0
        self.vibrato = vibrato

    def render(self, buf, wet, sr):
        n = len(buf)
        t_arr = self.t + np.arange(n, dtype=np.float32) / sr
        if self.vibrato:
            rate, cents = self.vibrato
            ramp = np.clip(t_arr / 0.20, 0, 1)
            f = self.freq * (2.0 ** (cents / 1200.0 * ramp * np.sin(2.0 * np.pi * rate * t_arr)))
        else:
            f = np.full(n, self.freq, dtype=np.float32)
        dphase = f / sr
        phases = (self.phase + np.cumsum(dphase)) % 1.0
        self.phase = float(phases[-1])
        idx = (phases * 32).astype(np.int32) % 32
        samples = self.wave[idx]
        env = self.env.render(n, sr, self.t)
        out = samples * env * self.vol
        if self.bus == 'wet':
            wet += out
        else:
            buf += out
        self.t += n / sr
        if self.t >= self.env.total():
            self.done = True


class NoiseVoice(_Voice):
    __slots__ = ('freq', 'env', 'vol', 'short', 'sample_offset')

    def __init__(self, freq, env, vol=0.3, short=False, bus='main'):
        super().__init__()
        self.bus = bus
        self.freq = freq
        self.env = env
        self.vol = vol
        self.short = short
        self.sample_offset = 0

    def render(self, buf, wet, sr):
        n = len(buf)
        period = max(1, int(sr / max(self.freq, 1.0)))
        table = LFSR_7 if self.short else LFSR_15
        sample_idx = self.sample_offset + np.arange(n, dtype=np.int64)
        lfsr_idx = (sample_idx // period) % len(table)
        samples = table[lfsr_idx]
        env = self.env.render(n, sr, self.t)
        out = samples * env * self.vol
        if self.bus == 'wet':
            wet += out
        else:
            buf += out
        self.sample_offset += n
        self.t += n / sr
        if self.t >= self.env.total():
            self.done = True


class DrumVoice(_Voice):
    """Composite drum voice: pitched body (with rapid pitch decay) + noise burst.

    This is what gives kicks a 'thump' and snares a 'snap' instead of just
    sounding like static. Body and noise have independent envelopes.
    """
    __slots__ = ('body_f0', 'body_f1', 'body_decay_t', 'body_vol',
                 'noise_freq', 'noise_short', 'noise_vol',
                 'env_body', 'env_noise', 'phase', 'sample_offset')

    def __init__(self, body_f0, body_f1, body_decay_t, body_vol,
                 noise_freq, noise_short, noise_vol,
                 env_body, env_noise):
        super().__init__()
        self.body_f0 = body_f0
        self.body_f1 = body_f1
        self.body_decay_t = max(body_decay_t, 1e-5)
        self.body_vol = body_vol
        self.noise_freq = noise_freq
        self.noise_short = noise_short
        self.noise_vol = noise_vol
        self.env_body = env_body
        self.env_noise = env_noise
        self.phase = 0.0
        self.sample_offset = 0

    def render(self, buf, wet, sr):
        n = len(buf)
        t_arr = self.t + np.arange(n, dtype=np.float32) / sr
        out = np.zeros(n, dtype=np.float32)

        # Body: sine-ish (pulse with PW=0.5) with exponential pitch fall
        if self.body_vol > 0:
            decay = np.exp(-t_arr / self.body_decay_t)
            f = self.body_f1 + (self.body_f0 - self.body_f1) * decay
            dphase = f / sr
            phases = (self.phase + np.cumsum(dphase)) % 1.0
            self.phase = float(phases[-1])
            # Use sine-ish body (less harsh than square) with PW=0.5
            body = np.sin(2 * np.pi * phases).astype(np.float32)
            env_b = self.env_body.render(n, sr, self.t)
            out += body * env_b * self.body_vol

        # Noise burst
        if self.noise_vol > 0:
            period = max(1, int(sr / max(self.noise_freq, 1.0)))
            table = LFSR_7 if self.noise_short else LFSR_15
            sample_idx = self.sample_offset + np.arange(n, dtype=np.int64)
            lfsr_idx = (sample_idx // period) % len(table)
            noise = table[lfsr_idx]
            env_n = self.env_noise.render(n, sr, self.t)
            out += noise * env_n * self.noise_vol
            self.sample_offset += n

        buf += out
        total = max(self.env_body.total(), self.env_noise.total())
        self.t += n / sr
        if self.t >= total:
            self.done = True


class ArpVoice(_Voice):
    """Game Boy 'arpeggio' trick — a single channel cycles fast through a
    chord, faking polyphony. Iconic chiptune flavor (Pokemon, Kirby)."""
    __slots__ = ('freqs', 'rate', 'duty', 'env', 'vol', 'phase')

    def __init__(self, freqs, rate_hz, env, duty=0.5, vol=0.3, bus='main'):
        super().__init__()
        self.bus = bus
        self.freqs = list(freqs)
        self.rate = rate_hz
        self.duty = duty
        self.env = env
        self.vol = vol
        self.phase = 0.0

    def render(self, buf, wet, sr):
        n = len(buf)
        t_arr = self.t + np.arange(n, dtype=np.float32) / sr
        nf = len(self.freqs)
        step = (t_arr * self.rate).astype(np.int64) % nf
        f = np.array(self.freqs, dtype=np.float32)[step]
        dphase = f / sr
        phases = (self.phase + np.cumsum(dphase)) % 1.0
        self.phase = float(phases[-1])
        samples = np.where(phases < self.duty, 1.0, -1.0).astype(np.float32)
        env = self.env.render(n, sr, self.t)
        out = samples * env * self.vol
        if self.bus == 'wet':
            wet += out
        else:
            buf += out
        self.t += n / sr
        if self.t >= self.env.total():
            self.done = True


# --- Engine -----------------------------------------------------------------
class AudioEngine:
    def __init__(self, sample_rate=SAMPLE_RATE, buffer_size=BUFFER_SIZE):
        self.sr = sample_rate
        self.bs = buffer_size
        self.lock = threading.Lock()
        self.voices = []
        self.muted = False
        self.master_vol = 0.55
        self.stream = None

        # Echo: ~3/16 of a 130bpm beat (≈ 173ms)
        self.echo_buf = np.zeros(int(sample_rate * 0.6), dtype=np.float32)
        self.echo_write = 0
        self.echo_delay_samples = int(sample_rate * 0.18)
        self.echo_feedback = 0.32
        self.echo_wet = 0.45    # how much of the wet bus to feed into echo
        self.echo_return = 0.55  # how loud the echo comes back into main

        # Soft limiter state (peak follower)
        self.limiter_gain = 1.0

    def start(self):
        if not AUDIO_AVAILABLE:
            return False
        try:
            self.stream = sd.OutputStream(
                samplerate=self.sr, channels=1, blocksize=self.bs,
                callback=self._cb, dtype='float32',
            )
            self.stream.start()
            return True
        except Exception:
            self.stream = None
            return False

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def add_voice(self, v):
        with self.lock:
            if len(self.voices) >= MAX_VOICES:
                return
            self.voices.append(v)

    def clear(self):
        with self.lock:
            self.voices.clear()
            self.echo_buf.fill(0.0)

    def _soft_limit(self, buf):
        # Look-ahead-free soft limiter with simple peak follower.
        peak = float(np.max(np.abs(buf)) + 1e-9)
        threshold = 0.85
        target = 1.0 if peak <= threshold else threshold / peak
        # smooth gain change (release ~30ms)
        a = 0.3
        if target < self.limiter_gain:
            self.limiter_gain = target  # immediate attack
        else:
            self.limiter_gain += a * (target - self.limiter_gain)
        out = buf * self.limiter_gain
        return np.tanh(out)

    def _cb(self, outdata, frames, time_info, status):
        buf = np.zeros(frames, dtype=np.float32)
        wet = np.zeros(frames, dtype=np.float32)

        if not self.muted:
            with self.lock:
                done = []
                for v in self.voices:
                    try:
                        v.render(buf, wet, self.sr)
                    except Exception:
                        v.done = True
                    if v.done:
                        done.append(v)
                for v in done:
                    self.voices.remove(v)

            # Tap-delay echo. The wet bus feeds the delay line; the delayed
            # signal is summed back into both `buf` (return mix) and into the
            # delay write (feedback) for repeating taps.
            L = len(self.echo_buf)
            read_start = (self.echo_write - self.echo_delay_samples) % L
            read_idx = (read_start + np.arange(frames)) % L
            delayed = self.echo_buf[read_idx]
            buf = buf + delayed * self.echo_return
            write_signal = wet * self.echo_wet + delayed * self.echo_feedback
            write_idx = (self.echo_write + np.arange(frames)) % L
            self.echo_buf[write_idx] = write_signal
            self.echo_write = (self.echo_write + frames) % L

            buf = np.convolve(buf, LPF_KERNEL, mode='same').astype(np.float32)
            buf = self._soft_limit(buf * self.master_vol).astype(np.float32)
        outdata[:, 0] = buf


# --- Scheduler --------------------------------------------------------------
class _Scheduler:
    def __init__(self):
        self._heap = []
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def schedule(self, delay, cb):
        with self._cv:
            heapq.heappush(self._heap, (time.monotonic() + delay, cb))
            self._cv.notify()

    def _run(self):
        while not self._stop:
            with self._cv:
                while not self._heap and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                next_t, _ = self._heap[0]
                wait = next_t - time.monotonic()
                if wait > 0:
                    self._cv.wait(wait)
                    continue
                _, cb = heapq.heappop(self._heap)
            try:
                cb()
            except Exception:
                pass


# --- Drum presets — pitched body + noise ------------------------------------
DRUM_PRESETS = {
    # kick: deep thump with strong pitch decay
    'kick':  dict(body_f0=180, body_f1=45, body_decay_t=0.04, body_vol=0.55,
                  noise_freq=2200, noise_short=True, noise_vol=0.10,
                  env_body=Envelope(0.001, 0.10, 0, 0.04, 0.16),
                  env_noise=Envelope(0.001, 0.015, 0, 0.005, 0.02)),
    # snare: short body tone + bright noise
    'snare': dict(body_f0=320, body_f1=180, body_decay_t=0.03, body_vol=0.20,
                  noise_freq=4500, noise_short=False, noise_vol=0.40,
                  env_body=Envelope(0.001, 0.05, 0, 0.02, 0.08),
                  env_noise=Envelope(0.001, 0.07, 0.15, 0.05, 0.13)),
    # hat: tight high-freq noise burst, no body
    'hat':   dict(body_f0=0, body_f1=0, body_decay_t=0.001, body_vol=0.0,
                  noise_freq=8000, noise_short=False, noise_vol=0.22,
                  env_body=Envelope(0.001, 0.002, 0, 0.002, 0.005),
                  env_noise=Envelope(0.001, 0.018, 0, 0.012, 0.032)),
    'ohat':  dict(body_f0=0, body_f1=0, body_decay_t=0.001, body_vol=0.0,
                  noise_freq=7500, noise_short=False, noise_vol=0.20,
                  env_body=Envelope(0.001, 0.002, 0, 0.002, 0.005),
                  env_noise=Envelope(0.001, 0.10, 0.2, 0.08, 0.20)),
    # tom: pitched mid kick
    'tom':   dict(body_f0=260, body_f1=140, body_decay_t=0.06, body_vol=0.45,
                  noise_freq=3500, noise_short=True, noise_vol=0.06,
                  env_body=Envelope(0.001, 0.10, 0.1, 0.06, 0.18),
                  env_noise=Envelope(0.001, 0.02, 0, 0.01, 0.03)),
    # crash: long bright noise
    'crash': dict(body_f0=0, body_f1=0, body_decay_t=0.001, body_vol=0.0,
                  noise_freq=9000, noise_short=False, noise_vol=0.30,
                  env_body=Envelope(0.001, 0.001, 0, 0.001, 0.002),
                  env_noise=Envelope(0.001, 0.4, 0.3, 0.6, 1.0)),
}


def make_drum(name, vol_mul=1.0):
    p = DRUM_PRESETS[name]
    return DrumVoice(
        body_f0=p['body_f0'], body_f1=p['body_f1'],
        body_decay_t=p['body_decay_t'], body_vol=p['body_vol'] * vol_mul,
        noise_freq=p['noise_freq'], noise_short=p['noise_short'],
        noise_vol=p['noise_vol'] * vol_mul,
        env_body=p['env_body'], env_noise=p['env_noise'],
    )


# --- Songs ------------------------------------------------------------------
def _bar(*notes):
    """Helper: pack a sequence of (note, beats) entries into a bar."""
    return list(notes)


# === TITLE ===
# C major, 130 BPM. 16 bars: A A' B B'. Chord progression
# A : C - Am - F - G  (I-vi-IV-V)
# A': same chords, ascending arc
# B : F - G - Em - Am (vi-IV-V flavor with ii cadence)
# B': F - G - C - C  (turnaround)
TITLE_LEAD = [
    # A
    ('C5', 0.5), ('E5', 0.5), ('G5', 0.5), ('rest', 0.5),
    ('C5', 0.5), ('G5', 0.5), ('E5', 1.0),
    ('A4', 0.5), ('C5', 0.5), ('E5', 0.5), ('rest', 0.5),
    ('A4', 0.5), ('E5', 0.5), ('C5', 1.0),
    ('F4', 0.5), ('A4', 0.5), ('C5', 0.5), ('rest', 0.5),
    ('F4', 0.5), ('C5', 0.5), ('A4', 1.0),
    ('G4', 0.5), ('B4', 0.5), ('D5', 0.5), ('rest', 0.5),
    ('G4', 0.5), ('D5', 0.5), ('B4', 1.0),
    # A' — climbing variation
    ('G5', 0.5), ('E5', 0.5), ('C5', 0.5), ('G4', 0.5),
    ('rest', 0.5), ('C5', 0.5), ('E5', 0.5), ('G5', 0.5),
    ('A5', 0.5), ('E5', 0.5), ('C5', 0.5), ('A4', 0.5),
    ('rest', 0.5), ('C5', 0.5), ('E5', 0.5), ('A5', 0.5),
    ('F5', 0.5), ('C5', 0.5), ('A4', 0.5), ('F4', 0.5),
    ('rest', 0.5), ('A4', 0.5), ('C5', 0.5), ('F5', 0.5),
    ('G5', 0.5), ('D5', 0.5), ('B4', 0.5), ('G4', 0.5),
    ('rest', 0.5), ('B4', 0.5), ('D5', 0.5), ('G5', 0.5),
    # B — singable hook in upper register
    ('F5', 0.5), ('A5', 0.5), ('C6', 0.5), ('A5', 0.5),
    ('F5', 0.5), ('G5', 0.5), ('A5', 1.0),
    ('G5', 0.5), ('B5', 0.5), ('D6', 0.5), ('B5', 0.5),
    ('G5', 0.5), ('A5', 0.5), ('B5', 1.0),
    ('E5', 0.5), ('G5', 0.5), ('B5', 0.5), ('G5', 0.5),
    ('E5', 0.5), ('F5', 0.5), ('G5', 1.0),
    ('A4', 0.5), ('C5', 0.5), ('E5', 0.5), ('G5', 0.5),
    ('A5', 0.5), ('G5', 0.5), ('E5', 0.5), ('C5', 0.5),
    # B' — descending resolution back to tonic
    ('F5', 0.5), ('G5', 0.5), ('A5', 0.5), ('G5', 0.5),
    ('F5', 0.5), ('E5', 0.5), ('D5', 0.5), ('C5', 0.5),
    ('G5', 0.5), ('F5', 0.5), ('E5', 0.5), ('D5', 0.5),
    ('G4', 0.5), ('B4', 0.5), ('D5', 0.5), ('G5', 0.5),
    ('G5', 0.5), ('E5', 0.5), ('G5', 0.5), ('C6', 0.5),
    ('G5', 0.5), ('E5', 0.5), ('C5', 1.0),
    ('C6', 0.5), ('rest', 0.5), ('G5', 0.5), ('rest', 0.5),
    ('E5', 0.5), ('rest', 0.5), ('C5', 1.0),
]
TITLE_HARMONY = [
    # 16 bars of triad arpeggios, 4 notes per bar
    *[('E4', 1.0), ('G4', 1.0), ('E4', 1.0), ('G4', 1.0)] * 1,  # C
    *[('C4', 1.0), ('E4', 1.0), ('C4', 1.0), ('E4', 1.0)] * 1,  # Am
    *[('A3', 1.0), ('C4', 1.0), ('A3', 1.0), ('C4', 1.0)] * 1,  # F
    *[('B3', 1.0), ('D4', 1.0), ('B3', 1.0), ('D4', 1.0)] * 1,  # G
    *[('E4', 1.0), ('G4', 1.0), ('C5', 1.0), ('G4', 1.0)] * 1,  # C (open)
    *[('C4', 1.0), ('E4', 1.0), ('A4', 1.0), ('E4', 1.0)] * 1,  # Am
    *[('A3', 1.0), ('C4', 1.0), ('F4', 1.0), ('C4', 1.0)] * 1,  # F
    *[('B3', 1.0), ('D4', 1.0), ('G4', 1.0), ('D4', 1.0)] * 1,  # G
    *[('A3', 1.0), ('C4', 1.0), ('F4', 1.0), ('C4', 1.0)] * 1,  # F
    *[('B3', 1.0), ('D4', 1.0), ('G4', 1.0), ('D4', 1.0)] * 1,  # G
    *[('G3', 1.0), ('B3', 1.0), ('E4', 1.0), ('B3', 1.0)] * 1,  # Em
    *[('A3', 1.0), ('C4', 1.0), ('E4', 1.0), ('C4', 1.0)] * 1,  # Am
    *[('A3', 1.0), ('C4', 1.0), ('F4', 1.0), ('C4', 1.0)] * 1,  # F
    *[('B3', 1.0), ('D4', 1.0), ('G4', 1.0), ('D4', 1.0)] * 1,  # G
    *[('E4', 1.0), ('G4', 1.0), ('C5', 1.0), ('G4', 1.0)] * 1,  # C
    *[('E4', 1.0), ('G4', 1.0), ('C5', 1.0), ('E5', 1.0)] * 1,  # C (climax)
]
TITLE_BASS = [
    ('C2', 1.0), ('G2', 1.0), ('C3', 1.0), ('G2', 1.0),
    ('A2', 1.0), ('E2', 1.0), ('A2', 1.0), ('E2', 1.0),
    ('F2', 1.0), ('C3', 1.0), ('F2', 1.0), ('C3', 1.0),
    ('G2', 1.0), ('D3', 1.0), ('G2', 1.0), ('D3', 1.0),
    ('C2', 1.0), ('E2', 1.0), ('G2', 1.0), ('E2', 1.0),
    ('A2', 1.0), ('C3', 1.0), ('E3', 1.0), ('C3', 1.0),
    ('F2', 1.0), ('A2', 1.0), ('C3', 1.0), ('A2', 1.0),
    ('G2', 1.0), ('B2', 1.0), ('D3', 1.0), ('B2', 1.0),
    ('F2', 1.0), ('A2', 1.0), ('F2', 1.0), ('A2', 1.0),
    ('G2', 1.0), ('B2', 1.0), ('G2', 1.0), ('B2', 1.0),
    ('E2', 1.0), ('G2', 1.0), ('B2', 1.0), ('G2', 1.0),
    ('A2', 1.0), ('C3', 1.0), ('A2', 1.0), ('C3', 1.0),
    ('F2', 1.0), ('A2', 1.0), ('C3', 1.0), ('A2', 1.0),
    ('G2', 1.0), ('B2', 1.0), ('D3', 1.0), ('B2', 1.0),
    ('C2', 1.0), ('G2', 1.0), ('E3', 1.0), ('G2', 1.0),
    ('C2', 1.0), ('G2', 1.0), ('C3', 2.0),
]
_DRUM_BAR_BASIC = [
    ('kick', 0.5), ('hat', 0.5), ('snare', 0.5), ('hat', 0.5),
    ('kick', 0.5), ('hat', 0.5), ('snare', 0.5), ('hat', 0.5),
]
_DRUM_BAR_GHOST = [
    ('kick', 0.5), ('hat', 0.5), ('snare', 0.5), ('hat', 0.25), ('kick', 0.25),
    ('kick', 0.5), ('hat', 0.5), ('snare', 0.5), ('ohat', 0.5),
]
_DRUM_FILL = [
    ('tom', 0.25), ('tom', 0.25), ('snare', 0.25), ('snare', 0.25),
    ('tom', 0.25), ('snare', 0.25), ('snare', 0.25), ('crash', 1.0),
]
TITLE_DRUMS = (
    _DRUM_BAR_BASIC * 3 + _DRUM_BAR_GHOST +
    _DRUM_BAR_BASIC * 3 + _DRUM_FILL +
    _DRUM_BAR_GHOST * 3 + _DRUM_FILL +
    _DRUM_BAR_BASIC * 2 + _DRUM_BAR_GHOST + _DRUM_FILL
)

TITLE_SONG = {
    'bpm': 130, 'loop': True,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.5,   'vol': 0.14, 'sustain': 0.5,
                    'attack': 0.005, 'release': 0.05,
                    'vibrato': (5.5, 14), 'pwm': (0.4, 0.10), 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.25,  'vol': 0.08, 'sustain': 0.45,
                    'attack': 0.005, 'release': 0.05},
        'bass':    {'kind': 'wave',  'wave': 'tri', 'vol': 0.22, 'sustain': 0.7,
                    'attack': 0.01,  'release': 0.05},
        'drums':   {'kind': 'drum',  'vol': 0.36},
    },
    'tracks': {
        'lead':    TITLE_LEAD,
        'harmony': TITLE_HARMONY,
        'bass':    TITLE_BASS,
        'drums':   TITLE_DRUMS,
    },
}


# Drum primitives ----------------------------------------------------------
# Each pattern is exactly 2 beats long (8 sixteenths). Combining them gives
# bar-level (4 beats) variations so the kit doesn't sound like a single loop.
_DRUM_FAST = [
    ('kick', 0.25), ('hat', 0.25), ('hat', 0.25), ('snare', 0.25),
    ('kick', 0.25), ('hat', 0.25), ('kick', 0.25), ('snare', 0.25),
]
_DRUM_FAST_VAR = [
    ('kick', 0.25), ('hat', 0.25), ('snare', 0.25), ('hat', 0.25),
    ('kick', 0.25), ('kick', 0.25), ('snare', 0.25), ('hat', 0.25),
]
_DRUM_HALF = [
    ('kick', 0.5),  ('hat', 0.5),
    ('snare', 0.5), ('hat', 0.5),
]
_DRUM_ROLL = [
    ('snare', 0.25), ('snare', 0.25), ('snare', 0.25), ('snare', 0.25),
    ('kick', 0.5),   ('snare', 0.5),
]


# === GAMEPLAY_A === A natural minor, 132 BPM, 32-bar composition.
#
# Form  : A (Am verse) — B (C major chorus) — A' (Am variation) — C (build,
#         resolve on E7 to loop cleanly back to Am)
# Bars  : 32 × 4 beats = 128 beats per loop ≈ 58 seconds.
# Chart : Am Am F  G  | Am Em F  E7 | C  G  F  C  | Dm G  C  C
#         Am Am Dm Em | Am F  Dm E7 | Am C  F  Am | Dm E7 Am E7

GAMEPLAY_A_LEAD = [
    # --- Section A (bars 1-8): the main motif over Am ---
    # 1  Am — root rocket
    ('A4', 0.5), ('C5', 0.5), ('E5', 0.5), ('A5', 0.5),
    ('E5', 0.5), ('C5', 0.5), ('B4', 0.5), ('A4', 0.5),
    # 2  Am — call & response, leave room
    ('A4', 1.0), ('rest', 0.5), ('C5', 0.5),
    ('E5', 0.5), ('C5', 0.5), ('A4', 1.0),
    # 3  F — same shape, new colour
    ('F4', 0.5), ('A4', 0.5), ('C5', 0.5), ('F5', 0.5),
    ('C5', 0.5), ('A4', 0.5), ('F4', 0.5), ('A4', 0.5),
    # 4  G — push toward the chorus
    ('G4', 0.5), ('B4', 0.5), ('D5', 0.5), ('G5', 0.5),
    ('D5', 0.5), ('B4', 0.5), ('G4', 0.5), ('B4', 0.5),
    # 5  Am — peak the phrase
    ('A4', 0.5), ('E5', 0.5), ('A5', 1.0),
    ('G5', 0.5), ('E5', 0.5), ('C5', 0.5), ('A4', 0.5),
    # 6  Em — sit on the v chord
    ('E4', 0.5), ('G4', 0.5), ('B4', 0.5), ('E5', 0.5),
    ('B4', 0.5), ('G4', 0.5), ('E4', 0.5), ('G4', 0.5),
    # 7  F — ascending tail
    ('F4', 0.5), ('A4', 0.5), ('C5', 1.0),
    ('F5', 0.5), ('E5', 0.5), ('D5', 0.5), ('C5', 0.5),
    # 8  E7 — V7 cliffhanger
    ('E4', 0.5), ('G#4', 0.5), ('B4', 1.0),
    ('D5', 0.5), ('C5', 0.5), ('B4', 1.0),

    # --- Section B (bars 9-16): C major chorus, brighter air ---
    # 9  C
    ('C5', 0.5), ('E5', 0.5), ('G5', 0.5), ('C6', 0.5),
    ('G5', 0.5), ('E5', 0.5), ('C5', 0.5), ('E5', 0.5),
    # 10 G — leap up
    ('D5', 0.5), ('G5', 0.5), ('B5', 1.0),
    ('A5', 0.5), ('G5', 0.5), ('F5', 0.5), ('D5', 0.5),
    # 11 F — wide leaps
    ('F5', 0.5), ('A5', 0.5), ('C6', 1.0),
    ('A5', 0.5), ('F5', 0.5), ('C5', 0.5), ('A4', 0.5),
    # 12 C — phrase peak
    ('G5', 0.5), ('E5', 0.5), ('C5', 1.0),
    ('E5', 0.5), ('G5', 0.5), ('C6', 1.0),
    # 13 Dm — pivot
    ('D5', 0.5), ('F5', 0.5), ('A5', 0.5), ('D6', 0.5),
    ('A5', 0.5), ('F5', 0.5), ('D5', 0.5), ('F5', 0.5),
    # 14 G
    ('G5', 0.5), ('B5', 0.5), ('D6', 1.0),
    ('B5', 0.5), ('G5', 0.5), ('D5', 0.5), ('B4', 0.5),
    # 15 C climbing
    ('C5', 0.5), ('E5', 0.5), ('G5', 0.5), ('C6', 0.5),
    ('B5', 0.5), ('A5', 0.5), ('G5', 0.5), ('E5', 0.5),
    # 16 C resolve
    ('C5', 2.0), ('G4', 1.0), ('C5', 1.0),

    # --- Section A' (bars 17-24): Am variation, melodic ---
    # 17 Am — new shape
    ('A4', 0.5), ('C5', 0.5), ('E5', 0.5), ('A5', 0.5),
    ('B5', 0.5), ('A5', 0.5), ('G5', 0.5), ('E5', 0.5),
    # 18 Am — scalewise climb
    ('A4', 1.0), ('B4', 0.5), ('C5', 0.5),
    ('D5', 0.5), ('E5', 0.5), ('F5', 0.5), ('G5', 0.5),
    # 19 Dm
    ('A5', 0.5), ('F5', 0.5), ('D5', 0.5), ('A4', 0.5),
    ('D5', 0.5), ('F5', 0.5), ('A5', 0.5), ('D6', 0.5),
    # 20 Em
    ('B5', 0.5), ('G5', 0.5), ('E5', 0.5), ('B4', 0.5),
    ('E5', 0.5), ('G5', 0.5), ('B5', 0.5), ('E6', 0.5),
    # 21 Am — apex
    ('C6', 0.5), ('A5', 0.5), ('E5', 0.5), ('A4', 0.5),
    ('E5', 0.5), ('A5', 0.5), ('B5', 0.5), ('C6', 0.5),
    # 22 F
    ('D6', 1.0), ('C6', 0.5), ('A5', 0.5),
    ('F5', 0.5), ('A5', 0.5), ('C6', 1.0),
    # 23 Dm
    ('D5', 0.5), ('F5', 0.5), ('A5', 0.5), ('F5', 0.5),
    ('D5', 0.5), ('A4', 0.5), ('D5', 0.5), ('F5', 0.5),
    # 24 E7
    ('E5', 0.5), ('G#5', 0.5), ('B5', 0.5), ('E6', 0.5),
    ('D6', 0.5), ('B5', 0.5), ('G#5', 0.5), ('B5', 0.5),

    # --- Section C (bars 25-32): breathy outro into final V7 ---
    # 25 Am breath
    ('A5', 1.0), ('rest', 0.5), ('E5', 0.5),
    ('C5', 1.0), ('A4', 1.0),
    # 26 C melodic descent
    ('C5', 0.5), ('E5', 0.5), ('G5', 0.5), ('C6', 0.5),
    ('B5', 0.5), ('G5', 0.5), ('E5', 0.5), ('C5', 0.5),
    # 27 F
    ('F5', 1.0), ('A5', 0.5), ('F5', 0.5),
    ('C5', 0.5), ('F5', 0.5), ('A5', 0.5), ('C6', 0.5),
    # 28 Am — long descending run
    ('A5', 0.5), ('G5', 0.5), ('F5', 0.5), ('E5', 0.5),
    ('D5', 0.5), ('C5', 0.5), ('B4', 0.5), ('A4', 0.5),
    # 29 Dm climb
    ('D5', 0.5), ('F5', 0.5), ('A5', 0.5), ('D6', 0.5),
    ('C6', 0.5), ('A5', 0.5), ('F5', 0.5), ('D5', 0.5),
    # 30 E7 tension
    ('E5', 0.5), ('G#5', 0.5), ('B5', 0.5), ('E6', 0.5),
    ('D6', 0.5), ('B5', 0.5), ('G#5', 0.5), ('E5', 0.5),
    # 31 Am — big hold
    ('A5', 2.0), ('E5', 1.0), ('C5', 1.0),
    # 32 E7 turnaround — leading-tone resolves into Am on the loop point
    ('E5', 0.5), ('B4', 0.5), ('G#4', 0.5), ('B4', 0.5),
    ('D5', 1.0), ('rest', 1.0),
]

# Harmony arpeggiates each chord across 4 beats (8 eighth notes). Built
# programmatically from the chord chart so the harmony always tracks the
# lead's chord progression.
_CHORDS_A = {
    'Am': ('A3', 'C4', 'E4', 'C4'),
    'F':  ('F3', 'A3', 'C4', 'A3'),
    'C':  ('C4', 'E4', 'G4', 'E4'),
    'G':  ('G3', 'B3', 'D4', 'B3'),
    'Em': ('E3', 'G3', 'B3', 'G3'),
    'Dm': ('D3', 'F3', 'A3', 'F3'),
    'E7': ('E3', 'G#3', 'B3', 'D4'),
}
_CHART_A = [
    'Am','Am','F', 'G',  'Am','Em','F', 'E7',
    'C', 'G', 'F', 'C',  'Dm','G', 'C', 'C',
    'Am','Am','Dm','Em', 'Am','F', 'Dm','E7',
    'Am','C', 'F', 'Am', 'Dm','E7','Am','E7',
]
GAMEPLAY_A_HARMONY = []
for _ch in _CHART_A:
    _arp = _CHORDS_A[_ch]
    # 8 eighth notes per bar: arpeggio up-down up-down
    for _n in _arp + _arp:
        GAMEPLAY_A_HARMONY.append((_n, 0.5))

# Bass: chord roots an octave below the harmony, half notes (2 per bar).
GAMEPLAY_A_BASS = []
for _i, _ch in enumerate(_CHART_A):
    _root = _CHORDS_A[_ch][0]
    # Drop an octave for bass register.
    _note, _oct = _root.rstrip('-0123456789'), int(_root[-1])
    _bass = f'{_note}{max(1, _oct - 1)}'
    # Hop to the fifth on the second half of the bar for movement.
    _fifth_note = _CHORDS_A[_ch][2]
    _fn, _fo = _fifth_note.rstrip('-0123456789'), int(_fifth_note[-1])
    _bass5 = f'{_fn}{max(1, _fo - 1)}'
    GAMEPLAY_A_BASS.append((_bass, 2.0))
    GAMEPLAY_A_BASS.append((_bass5 if _i % 2 == 1 else _bass, 2.0))

# Drums: alternate between two patterns to fight monotony. 32 bars × 2
# patterns/bar = 64 patterns. Every 8 bars insert a short snare roll.
GAMEPLAY_A_DRUMS = []
for _bar in range(32):
    GAMEPLAY_A_DRUMS.extend(_DRUM_FAST)
    if _bar % 8 == 7:
        GAMEPLAY_A_DRUMS.extend(_DRUM_ROLL)
    else:
        GAMEPLAY_A_DRUMS.extend(_DRUM_FAST_VAR)

GAMEPLAY_A_SONG = {
    'bpm': 132, 'loop': True,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.5,   'vol': 0.14, 'sustain': 0.5,
                    'attack': 0.003, 'release': 0.04,
                    'vibrato': (6.0, 9), 'pitch_env': (0.4, 0.025), 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.125, 'vol': 0.08, 'sustain': 0.45,
                    'attack': 0.003, 'release': 0.04},
        'bass':    {'kind': 'wave',  'wave': 'saw', 'vol': 0.20, 'sustain': 0.65,
                    'attack': 0.005, 'release': 0.04},
        'drums':   {'kind': 'drum',  'vol': 0.36},
    },
    'tracks': {
        'lead':    GAMEPLAY_A_LEAD,
        'harmony': GAMEPLAY_A_HARMONY,
        'bass':    GAMEPLAY_A_BASS,
        'drums':   GAMEPLAY_A_DRUMS,
    },
}


# === GAMEPLAY_B === F# natural minor, 144 BPM, 32-bar composition.
#
# Tone  : driving, slightly darker than A. More leaps in the lead, syncopated
#         rests for tension. Section B briefly opens up in A major (relative).
# Chart : F#m F#m D   E  | F#m C#m Bm  C#7 | A   E   D  A  | Bm  E   A  A
#         F#m F#m Bm  C#m| F#m D   Bm  C#7 | F#m A   D  F#m| Bm  C#7 F#m C#7

GAMEPLAY_B_LEAD = [
    # --- Section A: F#m groove ---
    # 1  F#m — define the riff
    ('F#4', 0.5), ('A4', 0.5), ('C#5', 0.5), ('F#5', 0.5),
    ('C#5', 0.5), ('A4', 0.5), ('B4', 0.5), ('F#4', 0.5),
    # 2  F#m — answer
    ('F#4', 1.0), ('rest', 0.5), ('A4', 0.5),
    ('C#5', 0.5), ('E5', 0.5), ('C#5', 1.0),
    # 3  D — open up briefly
    ('D5', 0.5), ('F#5', 0.5), ('A5', 0.5), ('D6', 0.5),
    ('A5', 0.5), ('F#5', 0.5), ('D5', 0.5), ('F#5', 0.5),
    # 4  E — pivot back
    ('E5', 0.5), ('G#5', 0.5), ('B5', 0.5), ('E6', 0.5),
    ('B5', 0.5), ('G#5', 0.5), ('E5', 0.5), ('G#5', 0.5),
    # 5  F#m peak
    ('F#5', 0.5), ('A5', 0.5), ('C#6', 1.0),
    ('B5', 0.5), ('A5', 0.5), ('G#5', 0.5), ('F#5', 0.5),
    # 6  C#m
    ('E5', 0.5), ('G#5', 0.5), ('C#6', 0.5), ('E6', 0.5),
    ('C#6', 0.5), ('G#5', 0.5), ('E5', 0.5), ('C#5', 0.5),
    # 7  Bm climb
    ('B4', 0.5), ('D5', 0.5), ('F#5', 1.0),
    ('A5', 0.5), ('G5', 0.5), ('F#5', 0.5), ('E5', 0.5),
    # 8  C#7 cliffhanger
    ('C#5', 0.5), ('F5', 0.5), ('G#5', 1.0),
    ('B5', 0.5), ('A5', 0.5), ('G#5', 1.0),

    # --- Section B (bars 9-16): A major chorus — relief ---
    # 9  A
    ('A4', 0.5), ('C#5', 0.5), ('E5', 0.5), ('A5', 0.5),
    ('E5', 0.5), ('C#5', 0.5), ('A4', 0.5), ('C#5', 0.5),
    # 10 E
    ('B4', 0.5), ('E5', 0.5), ('G#5', 1.0),
    ('F#5', 0.5), ('E5', 0.5), ('D5', 0.5), ('B4', 0.5),
    # 11 D
    ('D5', 0.5), ('F#5', 0.5), ('A5', 1.0),
    ('F#5', 0.5), ('D5', 0.5), ('A4', 0.5), ('F#4', 0.5),
    # 12 A
    ('E5', 0.5), ('C#5', 0.5), ('A4', 1.0),
    ('C#5', 0.5), ('E5', 0.5), ('A5', 1.0),
    # 13 Bm
    ('B4', 0.5), ('D5', 0.5), ('F#5', 0.5), ('B5', 0.5),
    ('F#5', 0.5), ('D5', 0.5), ('B4', 0.5), ('D5', 0.5),
    # 14 E
    ('E5', 0.5), ('G#5', 0.5), ('B5', 1.0),
    ('G#5', 0.5), ('E5', 0.5), ('B4', 0.5), ('G#4', 0.5),
    # 15 A — apex with rising scale
    ('A4', 0.5), ('B4', 0.5), ('C#5', 0.5), ('D5', 0.5),
    ('E5', 0.5), ('F#5', 0.5), ('G#5', 0.5), ('A5', 0.5),
    # 16 A — resolve
    ('A5', 2.0), ('E5', 1.0), ('A5', 1.0),

    # --- Section A' (bars 17-24): F#m, more agitated ---
    # 17 F#m new motif
    ('F#5', 0.5), ('rest', 0.5), ('F#5', 0.5), ('A5', 0.5),
    ('C#6', 0.5), ('A5', 0.5), ('F#5', 0.5), ('C#5', 0.5),
    # 18 F#m
    ('F#4', 0.5), ('A4', 0.5), ('C#5', 0.5), ('F#5', 0.5),
    ('A5', 0.5), ('F#5', 0.5), ('C#5', 0.5), ('A4', 0.5),
    # 19 Bm
    ('B4', 0.5), ('D5', 0.5), ('F#5', 0.5), ('B5', 0.5),
    ('D6', 0.5), ('B5', 0.5), ('F#5', 0.5), ('D5', 0.5),
    # 20 C#m
    ('C#5', 0.5), ('E5', 0.5), ('G#5', 0.5), ('C#6', 0.5),
    ('E6', 0.5), ('C#6', 0.5), ('G#5', 0.5), ('E5', 0.5),
    # 21 F#m — top
    ('F#5', 0.5), ('A5', 0.5), ('C#6', 0.5), ('F#6', 0.5),
    ('C#6', 0.5), ('A5', 0.5), ('F#5', 0.5), ('A5', 0.5),
    # 22 D
    ('D5', 0.5), ('F#5', 0.5), ('A5', 1.0),
    ('D6', 0.5), ('A5', 0.5), ('F#5', 0.5), ('D5', 0.5),
    # 23 Bm
    ('B4', 0.5), ('D5', 0.5), ('F#5', 0.5), ('D5', 0.5),
    ('B4', 0.5), ('A4', 0.5), ('B4', 0.5), ('D5', 0.5),
    # 24 C#7
    ('C#5', 0.5), ('F5', 0.5), ('G#5', 0.5), ('C#6', 0.5),
    ('B5', 0.5), ('G#5', 0.5), ('F5', 0.5), ('G#5', 0.5),

    # --- Section C (bars 25-32): build toward loop point ---
    # 25 F#m breath
    ('F#5', 1.0), ('rest', 0.5), ('C#5', 0.5),
    ('A4', 1.0), ('F#4', 1.0),
    # 26 A — surge
    ('A4', 0.5), ('C#5', 0.5), ('E5', 0.5), ('A5', 0.5),
    ('G#5', 0.5), ('E5', 0.5), ('C#5', 0.5), ('A4', 0.5),
    # 27 D
    ('D5', 1.0), ('F#5', 0.5), ('D5', 0.5),
    ('A4', 0.5), ('D5', 0.5), ('F#5', 0.5), ('A5', 0.5),
    # 28 F#m — long descent
    ('F#5', 0.5), ('E5', 0.5), ('D5', 0.5), ('C#5', 0.5),
    ('B4', 0.5), ('A4', 0.5), ('G#4', 0.5), ('F#4', 0.5),
    # 29 Bm climb
    ('B4', 0.5), ('D5', 0.5), ('F#5', 0.5), ('B5', 0.5),
    ('A5', 0.5), ('F#5', 0.5), ('D5', 0.5), ('B4', 0.5),
    # 30 C#7 tension
    ('C#5', 0.5), ('F5', 0.5), ('G#5', 0.5), ('C#6', 0.5),
    ('B5', 0.5), ('G#5', 0.5), ('F5', 0.5), ('C#5', 0.5),
    # 31 F#m — big hold
    ('F#5', 2.0), ('C#5', 1.0), ('A4', 1.0),
    # 32 C#7 turnaround — V7 to loop back to F#m
    ('C#5', 0.5), ('G#4', 0.5), ('F4', 0.5), ('G#4', 0.5),
    ('B4', 1.0), ('rest', 1.0),
]

# Harmony built from the chart, same arpeggio idea but a darker voicing.
_CHORDS_B = {
    'F#m': ('F#3', 'A3', 'C#4', 'A3'),
    'A':   ('A3', 'C#4', 'E4', 'C#4'),
    'D':   ('D3', 'F#3', 'A3', 'F#3'),
    'E':   ('E3', 'G#3', 'B3', 'G#3'),
    'C#m': ('C#3', 'E3', 'G#3', 'E3'),
    'Bm':  ('B3', 'D4', 'F#4', 'D4'),
    'C#7': ('C#3', 'F3', 'G#3', 'B3'),    # F = E# enharmonic for V7
}
_CHART_B = [
    'F#m','F#m','D', 'E',  'F#m','C#m','Bm','C#7',
    'A',  'E',  'D', 'A',  'Bm', 'E',  'A', 'A',
    'F#m','F#m','Bm','C#m','F#m','D',  'Bm','C#7',
    'F#m','A',  'D', 'F#m','Bm', 'C#7','F#m','C#7',
]
GAMEPLAY_B_HARMONY = []
for _ch in _CHART_B:
    _arp = _CHORDS_B[_ch]
    for _n in _arp + _arp:
        GAMEPLAY_B_HARMONY.append((_n, 0.5))

GAMEPLAY_B_BASS = []
for _i, _ch in enumerate(_CHART_B):
    _root = _CHORDS_B[_ch][0]
    _note, _oct = _root.rstrip('-0123456789'), int(_root[-1])
    _bass = f'{_note}{max(1, _oct - 1)}'
    _fifth_note = _CHORDS_B[_ch][2]
    _fn, _fo = _fifth_note.rstrip('-0123456789'), int(_fifth_note[-1])
    _bass5 = f'{_fn}{max(1, _fo - 1)}'
    GAMEPLAY_B_BASS.append((_bass, 2.0))
    GAMEPLAY_B_BASS.append((_bass5 if _i % 2 == 1 else _bass, 2.0))

GAMEPLAY_B_DRUMS = []
for _bar in range(32):
    GAMEPLAY_B_DRUMS.extend(_DRUM_FAST_VAR)
    if _bar % 8 == 7:
        GAMEPLAY_B_DRUMS.extend(_DRUM_ROLL)
    else:
        GAMEPLAY_B_DRUMS.extend(_DRUM_FAST)

GAMEPLAY_B_SONG = {
    'bpm': 144, 'loop': True,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.5,   'vol': 0.14, 'sustain': 0.5,
                    'attack': 0.003, 'release': 0.04,
                    'vibrato': (7.0, 12), 'pitch_env': (0.6, 0.020), 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.125, 'vol': 0.08, 'sustain': 0.45,
                    'attack': 0.003, 'release': 0.04},
        'bass':    {'kind': 'wave',  'wave': 'saw', 'vol': 0.22, 'sustain': 0.65,
                    'attack': 0.005, 'release': 0.04},
        'drums':   {'kind': 'drum',  'vol': 0.36},
    },
    'tracks': {
        'lead':    GAMEPLAY_B_LEAD,
        'harmony': GAMEPLAY_B_HARMONY,
        'bass':    GAMEPLAY_B_BASS,
        'drums':   GAMEPLAY_B_DRUMS,
    },
}


# === BOSS === level 10+, B minor, 144 BPM, half-time feel
BOSS_LEAD = [
    ('B4', 0.5), ('C5', 0.5), ('D5', 0.5), ('F5', 0.5),
    ('E5', 1.0), ('B4', 1.0),
    ('A4', 0.5), ('B4', 0.5), ('C5', 0.5), ('E5', 0.5),
    ('D5', 1.0), ('A4', 1.0),
    ('F5', 0.5), ('E5', 0.5), ('D5', 0.5), ('C5', 0.5),
    ('B4', 1.0), ('A4', 0.5), ('B4', 0.5),
    ('D5', 0.5), ('C5', 0.5), ('B4', 0.5), ('A4', 0.5),
    ('B4', 2.0),
]
BOSS_HARMONY = [
    ('D4', 1.0), ('F4', 1.0), ('B4', 1.0), ('F4', 1.0),
    ('A3', 1.0), ('C4', 1.0), ('E4', 1.0), ('C4', 1.0),
    ('G3', 1.0), ('B3', 1.0), ('D4', 1.0), ('B3', 1.0),
    ('F#3', 1.0), ('A3', 1.0), ('B3', 1.0), ('A3', 1.0),
]
BOSS_BASS = [
    ('B1', 0.5), ('B1', 0.5), ('F#2', 0.5), ('B1', 0.5),
    ('B1', 0.5), ('B1', 0.5), ('F#2', 0.5), ('B1', 0.5),
    ('A1', 0.5), ('A1', 0.5), ('E2', 0.5), ('A1', 0.5),
    ('A1', 0.5), ('A1', 0.5), ('E2', 0.5), ('A1', 0.5),
    ('G2', 0.5), ('G2', 0.5), ('D2', 0.5), ('G2', 0.5),
    ('F#2', 0.5), ('F#2', 0.5), ('A2', 0.5), ('F#2', 0.5),
    ('B1', 0.5), ('B1', 0.5), ('F#2', 0.5), ('B1', 0.5),
    ('B1', 1.0), ('F#2', 1.0),
]
BOSS_DRUMS = (
    [('kick', 1.0), ('snare', 1.0), ('kick', 0.5), ('kick', 0.5), ('snare', 1.0)] * 2 +
    [('kick', 0.5), ('kick', 0.5), ('snare', 1.0), ('tom', 0.5), ('tom', 0.5), ('snare', 1.0)] * 2
)

BOSS_SONG = {
    'bpm': 144, 'loop': True,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.25,  'vol': 0.24, 'sustain': 0.55,
                    'attack': 0.005, 'release': 0.06,
                    'vibrato': (5.0, 18), 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.5,   'vol': 0.12, 'sustain': 0.5,
                    'attack': 0.005, 'release': 0.06,
                    'pwm': (0.3, 0.12)},
        'bass':    {'kind': 'wave',  'wave': 'organ','vol': 0.36, 'sustain': 0.75,
                    'attack': 0.01, 'release': 0.06},
        'drums':   {'kind': 'drum',  'vol': 0.65},
    },
    'tracks': {
        'lead':    BOSS_LEAD,
        'harmony': BOSS_HARMONY,
        'bass':    BOSS_BASS,
        'drums':   BOSS_DRUMS,
    },
}


# === GAME OVER === slow descending, no loop
GAME_OVER_SONG = {
    'bpm': 84, 'loop': False,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.5,  'vol': 0.32, 'sustain': 0.6,
                    'release': 0.25, 'vibrato': (4.5, 22), 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.25, 'vol': 0.20, 'sustain': 0.5,
                    'release': 0.2},
        'bass':    {'kind': 'wave',  'wave': 'tri', 'vol': 0.34, 'sustain': 0.75,
                    'release': 0.4},
        'drums':   {'kind': 'drum',  'vol': 0.4},
    },
    'tracks': {
        'lead':    [('A4', 1), ('G4', 1), ('F4', 1), ('E4', 1),
                    ('D4', 1), ('C4', 1), ('B3', 1), ('A3', 2)],
        'harmony': [('E4', 1), ('D4', 1), ('C4', 1), ('B3', 1),
                    ('A3', 1), ('G3', 1), ('F3', 1), ('E3', 2)],
        'bass':    [('A2', 4), ('F2', 4), ('A2', 1)],
        'drums':   [('kick', 2), ('rest', 0), ('snare', 2), ('rest', 0),
                    ('kick', 2), ('snare', 2), ('crash', 1)],
    },
}


# === LEVEL CLEAR === fanfare, no loop
LEVEL_CLEAR_SONG = {
    'bpm': 140, 'loop': False,
    'channels': {
        'lead':    {'kind': 'pulse', 'duty': 0.5,  'vol': 0.30,
                    'release': 0.1, 'sustain': 0.5, 'echo': True},
        'harmony': {'kind': 'pulse', 'duty': 0.25, 'vol': 0.20,
                    'release': 0.1, 'sustain': 0.5},
        'bass':    {'kind': 'wave',  'wave': 'tri', 'vol': 0.30,
                    'release': 0.1, 'sustain': 0.7},
        'drums':   {'kind': 'drum',  'vol': 0.55},
    },
    'tracks': {
        'lead':    [('C5', 0.25), ('E5', 0.25), ('G5', 0.25), ('C6', 0.5),
                    ('B5', 0.25), ('C6', 1.0)],
        'harmony': [('E4', 0.25), ('G4', 0.25), ('C5', 0.25), ('E5', 0.5),
                    ('D5', 0.25), ('E5', 1.0)],
        'bass':    [('C3', 0.5), ('G3', 0.5), ('C3', 1.5)],
        'drums':   [('kick', 0.25), ('snare', 0.25), ('kick', 0.25),
                    ('snare', 0.25), ('crash', 1.5)],
    },
}


SONGS = {
    'TITLE':       TITLE_SONG,
    'GAMEPLAY_A':  GAMEPLAY_A_SONG,
    'GAMEPLAY_B':  GAMEPLAY_B_SONG,
    'BOSS':        BOSS_SONG,
    'GAME_OVER':   GAME_OVER_SONG,
    'LEVEL_CLEAR': LEVEL_CLEAR_SONG,
}


def gameplay_song_for(level):
    # Boss levels use the heavy BOSS theme.
    if level >= 5 and level % 5 == 0:
        return 'BOSS'
    # Alternate A/B every level so consecutive runs don't loop the same song.
    return 'GAMEPLAY_A' if level % 2 == 1 else 'GAMEPLAY_B'


# --- Sequencer --------------------------------------------------------------
class Sequencer(threading.Thread):
    def __init__(self, engine, song):
        super().__init__(daemon=True)
        self.engine = engine
        self.song = song
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        spb = 60.0 / self.song['bpm']
        ch_cfgs = self.song.get('channels', {})

        events = []
        track_total = 0.0
        for ch, pattern in self.song['tracks'].items():
            t = 0.0
            for entry in pattern:
                if len(entry) == 2:
                    note, beats = entry
                    vol = 1.0
                else:
                    note, beats, vol = entry
                if note != 'rest':
                    events.append((t, ch, note, beats * spb, vol))
                t += beats * spb
            if t > track_total:
                track_total = t
        events.sort(key=lambda e: e[0])

        while not self.stop_event.is_set():
            loop_start = time.monotonic()
            for t_evt, ch_name, note, dur, vol in events:
                if self.stop_event.is_set():
                    return
                wait = (loop_start + t_evt) - time.monotonic()
                if wait > 0:
                    if self.stop_event.wait(wait):
                        return
                self._fire(ch_name, ch_cfgs.get(ch_name, {}), note, dur, vol)
            remain = (loop_start + track_total) - time.monotonic()
            if remain > 0:
                if self.stop_event.wait(remain):
                    return
            if not self.song.get('loop'):
                return

    def _fire(self, ch_name, cfg, note, dur, vol):
        kind = cfg.get('kind', 'pulse')
        base_vol = cfg.get('vol', 0.3) * vol
        bus = 'main'
        if kind == 'pulse':
            env = Envelope(
                attack=cfg.get('attack', 0.005),
                decay=cfg.get('decay', 0.04),
                sustain=cfg.get('sustain', 0.5),
                release=cfg.get('release', 0.04),
                duration=dur,
            )
            v = PulseVoice(
                note_to_freq(note), cfg.get('duty', 0.5), env,
                vol=base_vol, vibrato=cfg.get('vibrato'),
                pwm=cfg.get('pwm'), pitch_env=cfg.get('pitch_env'),
            )
            self.engine.add_voice(v)
            if cfg.get('echo'):
                # Send the lead also to the wet bus for the tap-delay echo.
                v2 = PulseVoice(
                    note_to_freq(note), cfg.get('duty', 0.5),
                    Envelope(env.a, env.d, env.s, env.r, dur),
                    vol=base_vol * 0.6, vibrato=cfg.get('vibrato'),
                    pwm=cfg.get('pwm'), pitch_env=cfg.get('pitch_env'),
                    bus='wet',
                )
                self.engine.add_voice(v2)
        elif kind == 'wave':
            env = Envelope(
                attack=cfg.get('attack', 0.01),
                decay=cfg.get('decay', 0.08),
                sustain=cfg.get('sustain', 0.7),
                release=cfg.get('release', 0.05),
                duration=dur,
            )
            self.engine.add_voice(WaveVoice(
                note_to_freq(note), env, vol=base_vol,
                wave=cfg.get('wave', 'tri'),
                vibrato=cfg.get('vibrato'),
            ))
        elif kind == 'drum':
            if note in DRUM_PRESETS:
                self.engine.add_voice(make_drum(note, vol_mul=base_vol / 0.55))
        elif kind == 'noise':
            env = Envelope(0.001, 0.04, 0, 0.02, 0.06)
            self.engine.add_voice(NoiseVoice(note_to_freq(note), env, vol=base_vol))


# --- SFX --------------------------------------------------------------------
def _sfx_shoot(eng, sched):
    # Layered punch: noise tick + low body thump + rising pulse arpeggio.
    # The noise tick + body give the "kick" feel before the tonal arpeggio sells the harpoon launch.
    eng.add_voice(NoiseVoice(2400,
                             Envelope(0.001, 0.018, 0, 0.008, 0.028),
                             vol=0.18, short=True))
    eng.add_voice(PulseVoice(140, 0.5,
                             Envelope(0.001, 0.030, 0, 0.022, 0.055),
                             vol=0.22,
                             sweep=(70, 0.04),
                             pitch_env=(-3, 0.018)))
    for i in range(3):
        f = 760 + i * 300
        env = Envelope(0.001, 0.022, 0, 0.012, 0.04)
        sched.schedule(i * 0.012,
                       lambda f=f, env=env: eng.add_voice(
                           PulseVoice(f, 0.25, env, vol=0.22,
                                      pitch_env=(0.5, 0.010))))


def _sfx_pop(eng, size=2):
    base = 240 + (4 - size) * 280
    env  = Envelope(0.001, 0.05, 0.0, 0.05, 0.11)
    eng.add_voice(PulseVoice(base, 0.5, env, vol=0.30,
                             sweep=(base * 0.35, 0.10),
                             pitch_env=(0.5, 0.015)))
    # noise crack
    env2 = Envelope(0.001, 0.025, 0.0, 0.012, 0.045)
    eng.add_voice(NoiseVoice(1800 + (4 - size) * 700, env2, vol=0.13))
    # heft for big balls
    if size >= 3:
        eng.add_voice(make_drum('kick', vol_mul=0.6 + (size - 3) * 0.45))
    if size == 4:
        eng.add_voice(make_drum('crash', vol_mul=0.55))


def _sfx_combo(eng, level):
    """Pitched escalator — combo level shifts the lead pitch up. Adds harmonics
    above combo 3 to make the chain feel rewarding."""
    if level < 2:
        return
    base = 700
    f = base * (1.06 ** min(level - 1, 18))
    env = Envelope(0.001, 0.040, 0.3, 0.06, 0.14)
    eng.add_voice(PulseVoice(f, 0.5, env, vol=0.22,
                             pitch_env=(0.5, 0.010)))
    if level >= 3:
        eng.add_voice(PulseVoice(f * 1.5, 0.25,
                                 Envelope(0.001, 0.030, 0.3, 0.05, 0.12),
                                 vol=0.13))
    if level >= 6:
        eng.add_voice(PulseVoice(f * 2.0, 0.5,
                                 Envelope(0.001, 0.025, 0.2, 0.04, 0.10),
                                 vol=0.10))
    if level >= 9:
        eng.add_voice(NoiseVoice(7000,
                                 Envelope(0.001, 0.040, 0, 0.020, 0.07),
                                 vol=0.10))


def _sfx_powerup_spawn(eng, sched):
    """Descending chime when a powerup pops out of a ball."""
    for i, f in enumerate([1320, 990, 740]):
        env = Envelope(0.001, 0.05, 0.3, 0.08, 0.16)
        sched.schedule(i * 0.04,
                       lambda f=f, env=env: eng.add_voice(
                           WaveVoice(f, env, vol=0.16, wave='sine')))


def _sfx_powerup_warn(eng):
    """Tense beep when a powerup is about to disappear."""
    env = Envelope(0.001, 0.05, 0.2, 0.06, 0.15)
    eng.add_voice(PulseVoice(523, 0.5, env, vol=0.20,
                             pitch_env=(-1, 0.04)))


def _sfx_powerup_land(eng):
    """Soft thud when a falling powerup hits the floor."""
    eng.add_voice(make_drum('tom', vol_mul=0.55))


def _sfx_hit(eng):
    # a quick impact: pitched kick + bright noise
    eng.add_voice(make_drum('kick', vol_mul=2.6))
    env = Envelope(0.005, 0.18, 0.0, 0.10, 0.32)
    eng.add_voice(PulseVoice(220, 0.5, env, vol=0.30,
                             sweep=(50, 0.28), pitch_env=(-2, 0.05)))


def _sfx_shield_block(eng, sched):
    env = Envelope(0.001, 0.10, 0.3, 0.15, 0.30)
    eng.add_voice(PulseVoice(740, 0.5, env, vol=0.28,
                             pwm=(8.0, 0.2)))
    env2 = Envelope(0.001, 0.20, 0.0, 0.10, 0.30)
    eng.add_voice(NoiseVoice(2400, env2, vol=0.14))
    sched.schedule(0.05, lambda: eng.add_voice(
        PulseVoice(990, 0.25,
                   Envelope(0.001, 0.06, 0.4, 0.10, 0.20),
                   vol=0.22)))


def _sfx_life_up(eng, sched):
    notes = [659, 784, 1047, 1319]
    for i, f in enumerate(notes):
        d = 0.10 if i < 3 else 0.30
        env = Envelope(0.001, 0.04, 0.5, 0.05, d)
        sched.schedule(i * 0.085,
                       lambda f=f, env=env: eng.add_voice(
                           PulseVoice(f, 0.5, env, vol=0.28,
                                      pitch_env=(0.4, 0.012))))


def _sfx_bomb(eng, sched):
    # impact + low rumble + crash
    eng.add_voice(make_drum('kick', vol_mul=3.0))
    env = Envelope(0.001, 0.20, 0.4, 0.5, 0.7)
    eng.add_voice(PulseVoice(180, 0.5, env, vol=0.28, sweep=(35, 0.6)))
    env2 = Envelope(0.001, 0.30, 0.5, 0.6, 0.9)
    eng.add_voice(NoiseVoice(220, env2, vol=0.30, short=True))
    sched.schedule(0.04, lambda: eng.add_voice(make_drum('crash', vol_mul=1.4)))


def _sfx_powerup_kind(eng, sched, kind):
    if kind == 'L':
        _sfx_life_up(eng, sched)
        return
    if kind == 'B':
        _sfx_bomb(eng, sched)
        return
    if kind == 'D':
        for i, f in enumerate([988, 1319]):
            env = Envelope(0.001, 0.03, 0.0, 0.05, 0.08)
            sched.schedule(i * 0.06,
                           lambda f=f, env=env: eng.add_voice(
                               PulseVoice(f, 0.5, env, vol=0.30,
                                          pitch_env=(0.3, 0.01))))
        return
    if kind == 'S':
        env = Envelope(0.01, 0.4, 0.3, 0.20, 0.6)
        eng.add_voice(PulseVoice(880, 0.25, env, vol=0.28,
                                 sweep=(220, 0.5), pwm=(2.0, 0.2)))
        return
    if kind == 'F':
        # cluster: D minor 7 cluster (D F A C)
        for f in [294, 349, 440, 523]:
            env = Envelope(0.05, 0.20, 0.5, 0.30, 0.6)
            eng.add_voice(WaveVoice(f, env, vol=0.16, wave='sine',
                                    vibrato=(3.5, 30)))
        return
    if kind == 'W':
        # wide octaves
        for f in [262, 524, 1048]:
            env = Envelope(0.005, 0.06, 0.5, 0.10, 0.20)
            eng.add_voice(PulseVoice(f, 0.5, env, vol=0.18,
                                     pwm=(6.0, 0.15)))
        return
    if kind == 'X':
        # warm pad — three voices, slight detune
        for f, det in [(330, 0), (440, 4), (660, -3)]:
            env = Envelope(0.05, 0.15, 0.6, 0.40, 0.7)
            ff = f * (2 ** (det / 1200.0))
            eng.add_voice(WaveVoice(ff, env, vol=0.18, wave='organ',
                                    vibrato=(4.5, 12)))
        return
    if kind == 'K':
        # Sticky hook — metallic clang then a held resonant chime.
        env = Envelope(0.001, 0.03, 0.0, 0.05, 0.10)
        eng.add_voice(NoiseVoice(2000, env, vol=0.18))
        for i, f in enumerate([523, 784]):
            sched.schedule(0.04 + i * 0.05,
                           lambda f=f: eng.add_voice(
                               PulseVoice(f, 0.25,
                                          Envelope(0.001, 0.02, 0.3, 0.45, 0.6),
                                          vol=0.22, pwm=(2.5, 0.1))))
        return
    if kind == 'G':
        # Power gun — quick descending pulse train.
        for i, f in enumerate([784, 988, 1175]):
            env = Envelope(0.001, 0.03, 0.0, 0.04, 0.07)
            sched.schedule(i * 0.04,
                           lambda f=f, env=env: eng.add_voice(
                               PulseVoice(f, 0.5, env, vol=0.28,
                                          pitch_env=(0.4, 0.015))))
        return
    if kind == 'P':
        # Piercing — bright cut.
        for i, f in enumerate([880, 1175, 1568]):
            env = Envelope(0.001, 0.03, 0.0, 0.05, 0.10)
            sched.schedule(i * 0.05,
                           lambda f=f, env=env: eng.add_voice(
                               WaveVoice(f, env, vol=0.20, wave='tri')))
        return
    if kind == 'M':
        # Magnet — buzzy sine swept up.
        env = Envelope(0.005, 0.15, 0.6, 0.25, 0.4)
        eng.add_voice(WaveVoice(440, env, vol=0.22, wave='sine',
                                vibrato=(8.0, 40)))
        sched.schedule(0.10,
                       lambda: eng.add_voice(
                           WaveVoice(660, Envelope(0.005, 0.10, 0.5, 0.20, 0.3),
                                     vol=0.20, wave='sine',
                                     vibrato=(8.0, 40))))
        return
    if kind == 'R':
        # Mirror — chime that echoes higher.
        for i, f in enumerate([784, 1175]):
            env = Envelope(0.005, 0.06, 0.4, 0.18, 0.32)
            sched.schedule(i * 0.10,
                           lambda f=f, env=env: eng.add_voice(
                               WaveVoice(f, env, vol=0.22, wave='sine')))
        return
    if kind == 'T':
        # Triple — three quick stacked pulses (arpeggio).
        for i, f in enumerate([659, 880, 1175]):
            env = Envelope(0.001, 0.03, 0.0, 0.05, 0.10)
            sched.schedule(i * 0.03,
                           lambda f=f, env=env: eng.add_voice(
                               PulseVoice(f, 0.5, env, vol=0.25)))
        return
    # generic
    notes = [523, 659, 784, 988]
    for i, f in enumerate(notes):
        env = Envelope(0.001, 0.04, 0.4, 0.06, 0.12)
        sched.schedule(i * 0.04,
                       lambda f=f, env=env: eng.add_voice(
                           PulseVoice(f, 0.5, env, vol=0.25)))


def _sfx_select(eng, sched):
    for i, f in enumerate([659, 1047]):
        env = Envelope(0.001, 0.03, 0.0, 0.04, 0.08)
        sched.schedule(i * 0.05,
                       lambda f=f, env=env: eng.add_voice(
                           PulseVoice(f, 0.5, env, vol=0.28,
                                      pitch_env=(0.5, 0.01))))


def _sfx_wall_tick(eng):
    # very subtle tick when a ball hits the wall
    env = Envelope(0.001, 0.012, 0.0, 0.008, 0.025)
    eng.add_voice(NoiseVoice(6000, env, vol=0.06))


# --- Public API -------------------------------------------------------------
class _RealAudioManager:
    def __init__(self):
        self.engine = AudioEngine()
        self.sched  = _Scheduler()
        self.sequencer = None
        self.muted = False
        self.current_song = None
        self._ok = False

    def start(self):
        self._ok = self.engine.start()
        return self._ok

    def shutdown(self):
        self.stop_music()
        self.engine.stop()
        self._ok = False

    def play_music(self, name):
        if not self._ok:
            return
        if self.current_song == name and self.sequencer and self.sequencer.is_alive():
            return
        self.stop_music()
        song = SONGS.get(name)
        if not song:
            return
        self.engine.clear()
        self.sequencer = Sequencer(self.engine, song)
        self.sequencer.start()
        self.current_song = name

    def stop_music(self):
        if self.sequencer:
            self.sequencer.stop()
            self.sequencer = None
        self.current_song = None
        if self.engine:
            self.engine.clear()

    def sfx(self, name, **kwargs):
        if not self._ok:
            return
        e, s = self.engine, self.sched
        if   name == 'shoot':           _sfx_shoot(e, s)
        elif name == 'pop':             _sfx_pop(e, size=kwargs.get('size', 2))
        elif name == 'hit':             _sfx_hit(e)
        elif name == 'shield':          _sfx_shield_block(e, s)
        elif name == 'life_up':         _sfx_life_up(e, s)
        elif name == 'bomb':            _sfx_bomb(e, s)
        elif name == 'select':          _sfx_select(e, s)
        elif name == 'wall':            _sfx_wall_tick(e)
        elif name == 'combo':           _sfx_combo(e, kwargs.get('level', 1))
        elif name == 'powerup_spawn':   _sfx_powerup_spawn(e, s)
        elif name == 'powerup_warn':    _sfx_powerup_warn(e)
        elif name == 'powerup_land':    _sfx_powerup_land(e)
        elif name == 'powerup':         _sfx_powerup_kind(e, s, kwargs.get('kind', '?'))

    def toggle_mute(self):
        self.muted = not self.muted
        if self.engine:
            self.engine.muted = self.muted

    def is_muted(self):
        return self.muted

    def is_ok(self):
        return self._ok


class _StubAudioManager:
    def __init__(self): self.muted = False
    def start(self): return False
    def shutdown(self): pass
    def play_music(self, name): pass
    def stop_music(self): pass
    def sfx(self, name, **kwargs): pass
    def toggle_mute(self): self.muted = not self.muted
    def is_muted(self): return self.muted
    def is_ok(self): return False


def make_audio():
    if AUDIO_AVAILABLE:
        return _RealAudioManager()
    return _StubAudioManager()


if __name__ == '__main__':
    audio = make_audio()
    if not audio.start():
        print("Audio init failed.")
        raise SystemExit(1)
    print("Smoke test —")
    print("  TITLE 12s")
    audio.play_music('TITLE');       time.sleep(12)
    print("  GAMEPLAY_A 8s")
    audio.play_music('GAMEPLAY_A');  time.sleep(8)
    print("  GAMEPLAY_B 6s")
    audio.play_music('GAMEPLAY_B');  time.sleep(6)
    print("  BOSS 6s")
    audio.play_music('BOSS');        time.sleep(6)
    print("  SFX bursts")
    audio.sfx('shoot');              time.sleep(0.18)
    audio.sfx('pop', size=4);        time.sleep(0.30)
    audio.sfx('pop', size=2);        time.sleep(0.20)
    audio.sfx('powerup', kind='L');  time.sleep(0.50)
    audio.sfx('powerup', kind='X');  time.sleep(0.70)
    audio.sfx('shield');             time.sleep(0.50)
    audio.sfx('hit');                time.sleep(0.50)
    audio.sfx('bomb');               time.sleep(1.0)
    print("  GAME_OVER 8s")
    audio.play_music('GAME_OVER');   time.sleep(8)
    audio.shutdown()
    print("Done.")
