# Lucid Triton TRI054S-C — camera white paper (DRAFT v10, 2026-10-23)

What this camera is, how it works, how it behaves and how well it performs, measured on the
camera_lab bench. Machine-readable twin: `TRI054S-C.yaml`.

**Conditions:** all 24-bit (`raw_hdr`) numbers are at **Gain 0 dB and BlackLevel 1**, which
the `raw_hdr` mode pins. In this mode `Gain` is a digital multiplier after the HDR merge:
any other value scales every output level and clips highlights (see §8).

Tags: **[measured date]** our bench · **[manual]** Lucid documentation · **[to measure]**
the next fingerprint run fills it in. Numbers of one unit (hot pixels, exact black level)
are in that serial's fingerprint: `WS/config/camera_fingerprints/<serial>/`.

## 1. Summary
A 5.4 MP colour HDR camera built around Sony's IMX490 automotive sensor. Each pixel has two
sub-pixels read at two conversion gains; the sensor merges these four signals on the chip
into one linear 24-bit image — measured **137 dB (22.7 stops)** of dynamic range, above the
datasheet's > 120 dB — so very bright and very dark areas are captured in a single exposure. It has a rolling shutter. Out of the
box the camera applies a gamma-0.5 lookup table, which makes its output non-linear; its
linear raw data needs that table switched off. [manual], [measured 2026-10-22]

## 2. Architecture and signal path
| stage | what happens | notes |
|---|---|---|
| sensor | Sony IMX490, Bayer RGGB, rolling shutter, 5.4 MP | [manual] |
| HDR capture | per pixel: sub-pixel 1 (large) and sub-pixel 2 (small), each read at high and low conversion gain → 4 channels | [manual] |
| channel gains | analog gain per channel; factory values 19.8 / 4.2 / 29.4 / 4.2 dB (ch0–ch3) | [measured 2026-10-21] |
| on-chip merge | the 4 channels combined into one linear 24-bit value per pixel | [manual], linear [measured 2026-10-22] |
| LUT | lookup table, **on by default, gamma 0.5** (`LUTEnable`, `LUTToneMapping = GammaPointFive`) | [measured 2026-10-22] |
| colour transformation | colour matrix, on by default | [manual] |
| output | Bayer 8/12/16/24-bit, RGB8 (debayered in the camera), or one HDR channel alone (`HDROutput = HDRChannel0…3`, 12-bit) | [manual], [measured 2026-10-22] |

## 3. Output formats and their meaning
| output | linear? | use |
|---|---|---|
| BayerRG24, LUT off, colour transformation off | **yes** (×2.00 signal per exposure doubling) | the raw HDR data: measurement, detection |
| any format with the factory LUT on | no (×1.41 per doubling: gamma 0.5) | display only |
| RGB8 | no (LUT + debayer in the camera) | display only |
| HDRChannel0…3, 12-bit | per channel; see section 7 | diagnosis of the sensor |

`BlackLevel` (0–12.5 %, 0 by default) adds its % of 4095 to the black of every output
(1 → +40 DN, 5 → +205 DN on the 24-bit output at 0 dB). At the default 0 the black sits at
≈ 0.7 DN and 64 % of dark pixels read exactly 0: dark noise is clipped. `raw_hdr` sets 1
(black ≈ 40 DN, nothing clipped, a negligible 40 of 16.7 M). [measured 2026-10-23]

## 4. Controls and their behaviour
| control | behaviour |
|---|---|
| exposure | sets all four HDR channel exposures together while HDR tuning is off; 86 µs minimum; beyond one frame period it lowers the frame rate | [measured 2026-10-21/22] |
| gain | HDR tuning **off** (`raw_hdr`): `Gain` is a **digital multiplier after the merge** — no SNR gain, it scales the output and clips the top (28.6 dB costs 4.75 stops); keep 0 dB. HDR tuning on: `ExposureTime` and `Gain` become read-only and the per-channel exposures/gains apply | [measured 2026-10-21/23] |
| black level | `BlackLevel` shifts the black of every output by its % of 4095; keep ≥ 1 so dark noise is not clipped at 0 | [measured 2026-10-23] |
| HDR tuning (`HDRTuningEnable`) | off: one exposure for all channels; on: per-channel exposure and gain | [measured 2026-10-21] |
| `HDROutput`, `LUTEnable`, pixel format | **must be changed with the stream stopped**: written while streaming they are silently ignored | [measured 2026-10-22] |
| user sets | Default + UserSet1/2; a user set can be the boot set; it does **not** store HDR tuning or the HDR channel values | [measured 2026-10-21] |
| power-up state | boots from its boot user set with HDR tuning **off** and channel gains at the factory values | [measured 2026-10-21] |

### Setting names (camera_lab key ↔ GenICam node)
| camera_lab | GenICam node | notes |
|---|---|---|
| `exposure_us` | `ExposureTime` | µs; read-only while HDR tuning is on |
| `gain_db` | `Gain` | dB; digital after the merge in `raw_hdr` (keep 0); read-only while HDR tuning is on |
| `pixel_format` | `PixelFormat` | stream stop required |
| `frame_rate_hz` | `AcquisitionFrameRate` | needs `AcquisitionFrameRateEnable` |
| `white_balance` (red/green/blue) | `BalanceRatio` + `BalanceRatioSelector` | multiplies the raw data too: keep 1.0 for measurement |
| `hdr.enabled` | `HDRTuningEnable` | |
| `hdr.output` | `HDROutput` (`HDR`, `HDRChannel0…3`) | stream stop required |
| `hdr.enhancement` | `HDRImageEnhancementEnable` | |
| `hdr.clamping` | `HDRDigitalClampingEnable` | |
| `hdr.chN.exposure_us` | `HDRChannelExposureTime` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `hdr.chN.gain_db` | `HDRChannelAnalogGain` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `lut.enabled` | `LUTEnable` | stream stop required |
| `lut.tone_mapping` | `LUTToneMapping` (`Linear`, `GammaPointFive`, …) | |
| `color.transformation` | `ColorTransformationEnable` | |
| `black.level` | `BlackLevel` | % of 4095 added to the black of every output |
| profile mode `raw_hdr` | — | BayerRG24 + `gain_db` 0 + white balance 1/1/1 + `hdr.enabled` off + `hdr.output` HDR + `lut.enabled` off + `color.transformation` off + `black.level` 1 |

### Start-up state: never trust the boot state
A camera boots from whatever user set it was told to: someone's old settings (on our bench a
stray 28.6 dB digital gain skewed a day of measurements). **Every tool starts the same way:**
load the factory **Default** user set (live only, nothing written to flash), apply the
**complete** design-mode settings, read every one back, then verify on the image.
camera_lab: `camera-lab two-d reset <camera>` / MCP `reset_camera` (the site light
assessment and the fingerprint do it themselves).

**Factory Default** (`UserSetSelector = Default` → `UserSetLoad`) [measured 2026-10-23]:

| setting | factory Default |
|---|---|
| pixel format | BayerRG8 |
| exposure | 30 ms (auto off); HDR channel exposures follow it |
| gain | 0 dB (auto off) |
| BlackLevel | 0 |
| LUT | on, GammaPointFive (gamma 0.5) |
| colour transformation | on |
| HDR tuning / output | off / HDR; image enhancement and digital clamping on |
| HDR channel gains | 19.8 / 4.2 / 29.4 / 4.2 dB |
| white balance | **auto (Continuous)**, ratios ≈ 1.01 / 1.07 / 1.0 |
| frame rate | free run (19.7 fps at BayerRG8) |
| ROI / binning / trigger | full frame 2880 × 1860 / 1 / off |

**Design mode `raw_hdr` — the complete settings** (applied on top of Default):
BayerRG24, gain 0 dB, white balance 1.0 / 1.0 / 1.0 (auto off), LUT off, colour transformation
off, HDR tuning off, HDR output HDR, BlackLevel 1; everything else as Default.

**Verify** (§10): settings read back equal; on the image: doubling the exposure gives × 2.00;
the finest shadow step is 1 DN (no digital gain); no dark pixel at exactly 0.

White balance multiplies the raw 24-bit data (black included) even with the colour
transformation off: keep it at 1.0 and balance colour on the PC. [measured 2026-10-23]

## 5. Timing
| | value | source |
|---|---|---|
| frame rate, full frame | 20.8 fps BayerRG8; **6.93 fps BayerRG24** (the linear HDR output: 16 MB per frame fills GigE at ≈ 111 MB/s — use an ROI for more speed); other formats **[to measure]** | [measured 2026-10-21/23] |
| exposure vs frame rate | 61 ms → 16 fps, 122 ms → 8 fps, 244 ms → 4 fps | [measured 2026-10-21] |
| shutter | rolling: rows are exposed one after another over one readout (≈ one frame period) | [manual] |
| row (line) time | ≈ 23.5 µs per sensor row → ≈ 44 ms to read a full frame top to bottom | [measured 2026-10-23, from 120 Hz flicker bands] |
| triggered acquisition | max trigger rate ≈ half the free-run rate (a reset frame per trigger) | [manual] |
| trigger-to-image delay | **[to measure]** | |
| new settings visible after | 2 frame periods (camera_lab waits for them: 0 stale frames) | [measured 2026-10-22] |

## 6. Measured performance (linear 24-bit output, Gain 0 dB, BlackLevel 1)
Full scale = 2²⁴ − 1 = 16 777 215 DN. "Runs" = how many measurement runs the value rests on.

| property | value | fraction of full scale | runs / units | source |
|---|---|---|---|---|
| black level | ≈ 40 DN (BlackLevel 1); ≈ 0.7 DN at BlackLevel 0, noise clipped | 0.00024 % | 1 / 1 | [measured 2026-10-23] |
| read noise (dark, temporal) | **2.4 DN** at 1–10 ms, 3.3 DN at 100 ms | 0.000014 % | 1 / 1 | [measured 2026-10-23] |
| row noise (temporal, dark) | 0.3 DN rms, independent of exposure (≈ 1/8 of the pixel noise) | | 1 / 1 | [measured 2026-10-23] |
| DSNU | 0.6 DN at 1–10 ms, 1.0 DN at 100 ms | | 1 / 1 | [measured 2026-10-23] |
| **dynamic range** (saturation ÷ read noise) | **137 dB (22.7 stops)**, 134 dB at 100 ms — above the datasheet's > 120 dB | | 1 / 1 | [measured 2026-10-23] |
| saturation | the full 2²⁴ scale is reached (sensor limit, not a digital clip) | 100 % | 1 / 1 | [measured 2026-10-23] |
| dark current | ≈ 0 (black 40.7 → 40.3 DN from 1 to 100 ms) | | 1 / 1 | [measured 2026-10-23] |
| linearity error | 0.24 % of range | | 1 / 1 | [measured 2026-10-22, at 28.6 dB; gain-independent] |
| pixel response non-uniformity | 1.3 % | | 1 / 1 | [measured 2026-10-22; gain-independent] |
| vignetting | corners 0.90 of centre (lens- and iris-dependent) | | 1 / 1 | [measured 2026-10-22] |
| SNR vs signal level | **not monotonic** — see the curve below | | 1 / 1 | [measured 2026-10-23] |
| noise model σ² = a + b·signal | per channel regime **[to measure]** | | | |
| colour: R/G, B/G, green imbalance | **[to measure]** under the bench light (DC LED floods) | | | |
| run-to-run spread | **[to measure]** (3 runs per measurement) | | | |
| hot pixels | unit-specific (fingerprint per serial) | | | |

### SNR and effective resolution vs signal level (Gain 0 dB) [measured 2026-10-23, one run]
| level (% of full scale) | quantization step | channel in use | temporal noise σ | SNR |
|---|---|---|---|---|
| 0.03 % | 1 DN | ch0 | 162 DN | 27 |
| 0.05 % | 20 DN | ch1 | 227 DN | 39 |
| 0.2 % | 20 DN | ch1 | 453 DN | 80 |
| **0.4 %** | handover | ch1 → ch2 | 3 231 DN | **22 (dip)** |
| 0.8 % | 153 DN | ch2 | 4 210 DN | 33 |
| 1.7 % | 153 DN | ch2 | 5 801 DN | 48 |
| **3.3 %** | handover | ch2 → ch3 | 13 453 DN | **42 (dip)** |
| 6.7 % | 5 723 DN | ch3 | 18 148 DN | 62 |
| 13 % | 5 723 DN | ch3 | 21 820 DN | 104 |
| 27 % | 5 723 DN | ch3 | 28 117 DN | 161 |
| **53 %** | 5 723 DN | ch3 | 37 528 DN | **235** |
| **79 %** | 5 723 DN | ch3 | 46 261 DN | **287 (best)** |
| 100 % | saturation | | | |

- Four regimes, one per HDR channel: the step is one code of the channel in use (1, 20,
  153, 5 723 DN). The step ratios (20, 7.65, 37.4) are the channel sensitivity ratios and
  agree with the per-channel light measurements (22, 7.8, 36). The step is always far below
  the noise, so quantization never limits the data.
- **SNR dips at each handover** (≈ 0.4 % and ≈ 3.3 % of full scale) and is **best near the
  top** (≈ 235–290 at 50–80 %). Bright parts of a scene are the cleanest; parts sitting at a
  handover level are noisier than parts slightly darker.
- With digital gain the whole table shifts: at 28.6 dB every level is × 26.9, the handovers
  appear at ≈ 10 % and the top 4.75 stops clip (the 2026-10-23 morning numbers in the
  findings were taken that way).

## 7. The four HDR channels
| channel | sub-pixel / conversion gain | sensitivity vs ch3 | gain DN/e⁻ | full well e⁻ | read noise DN |
|---|---|---|---|---|---|
| ch0 | large, high (most sensitive) | ≈ 5700× (20× ch1, from the 0 dB quantization steps) | **[to measure]** | **[to measure]** | 67 |
| ch1 | large, low | ≈ 280× (7.8× ch2) | 27.0* | 145* | 20 |
| ch2 | small, high | ≈ 36× | 26.8–27.3* | 143* | 231 |
| ch3 | small, low (carries the highlights) | 1× | 0.08 or 0.72* | 48 840 or 5 427* | 71 |

\* Measured under flickering light at short exposures (2026-10-22): not reliable; to be
re-measured with the floods recipe (f/1.4 ch3, f/8 ch2, f/16 ch1, ≥ 2 ms).

The sensitivities span ≈ 6000× from ch3 to ch0 (≈ 76 dB), which is how the merged image
reaches its dynamic range: ch0 resolves the shadows, ch3 the highlights. Ratios measured two
independent ways agree: ch1/ch2 = 7.8 (light per channel) vs 7.2 (quantization steps).
[measured 2026-10-22/23]. Measuring ch0's photon transfer needs very little light (≈ 15× less
than a Kowa 8 mm at f/16 under bright diffuse light): an ND filter or extra diffusion.

## 8. Limitations and known behaviours
- **Digital gain in `raw_hdr`:** `Gain` multiplies after the merge; anything above 0 dB
  only scales the output and clips the highlights. A user set left at 28.6 dB silently cost
  4.75 stops on our bench until `raw_hdr` pinned 0 dB. [measured 2026-10-23]
- **BlackLevel 0 clips dark noise** (64 % of dark pixels read 0); use ≥ 1.
  [measured 2026-10-23]
- **Rolling shutter:** a subject moving during the exposure and readout is skewed (≈ 44 ms
  from the top row to the bottom row).
- **Light ripple becomes row bands.** Any light whose intensity ripples faster than the
  exposure shows as horizontal bands that move between frames. The band period in rows ×
  23.5 µs = the ripple period. Measured on the bench: dimmed LED panels 120 Hz (≈ 354 sensor
  rows per cycle; bands 2–3 % of the signal at 0.5–4 ms, gone at 8.3 ms = one mains half-cycle),
  "DC" LED floods ≈ 4 kHz (≈ 10 rows; 8 % at 0.1 ms, < 0.3 % from 2 ms). An exposure that is
  a whole number of ripple periods cancels it; longer exposures average it.
  [measured 2026-10-23]
- **Non-linear by default:** the factory LUT (gamma 0.5) is on; linear data needs
  `raw_hdr`-style settings (LUT off, colour transformation off, BayerRG24).
- **Stream-locked controls are ignored, not refused,** when written while streaming.
- **HDR settings do not survive a power cycle** through user sets.
- **Frame rate falls** when exposure exceeds one frame period, and halves with triggering.
- **8-bit output hides the noise floor** (noise below one 8-bit step at low gain).
- An SNR rise at 18 dB gain (vs 12 dB) was seen on the LUT output; likely a LUT effect.
  [measured 2026-10-21]

## 9. Diagnostic signatures (what a misconfigured or failing camera looks like)
| signature | meaning |
|---|---|
| doubling exposure gives × 1.41, not × 2.00 | factory LUT is on (gamma 0.5) |
| the finest step between pixel values in the shadows is > 1 DN (e.g. 26) | digital gain is on (step ≈ 10^(gain/20)); set `gain_db` 0 |
| many dark pixels read exactly 0 | BlackLevel 0: dark noise clipped; set `black.level` ≥ 1 |
| the 24-bit output clips at full scale while the scene is not that bright | digital gain is on |
| a written setting reads back changed but the image does not change | it was written while streaming (stream-locked control) |
| HDR tuning off and channel values back to factory after a power cycle | normal: user sets do not store HDR |
| `ExposureTime` / `Gain` read-only | HDR tuning is on |
| frame rate lower than expected | exposure longer than one frame period, or triggered acquisition |
| horizontal bands moving between frames | rippling light (rolling shutter): band period in rows × 23.5 µs = the light's ripple period (≈ 354 rows: 120 Hz mains; ≈ 10 rows: a switching LED driver) |
| slanted or wobbly vertical edges | subject moved during the exposure / readout |
| black level or read noise far from section 6 with the cap on | camera fault or heat (read `DeviceTemperature`) |
| hot-pixel count far above the unit's fingerprint | sensor ageing or damage |
| 8-bit output looks noise-free | normal: 8-bit hides the noise floor |

## 10. Checking the camera's state
| test | method | expected |
|---|---|---|
| linear output | steady scene, exposure E and 2E (5–40 % of full scale) | signal × 1.95–2.05 (× 1.41 = LUT on) |
| dark | lens cap on, 10 ms, Gain 0, BlackLevel 1 | black ≈ 40 DN, read noise ≈ 2.4 DN, no pixel at 0 |
| no digital gain | finest step between shadow values | 1 DN |
| start-up | `two-d reset` / `reset_camera` | all design-mode settings read back equal |
| hot pixels | lens cap on, 100 ms | close to the unit's fingerprint count |
| settling | change exposure, grab 3 frames | 0 stale frames |

## 11. Open questions
- **Light level differs between bench days** (3.6× at the same dimmer setting): the
  absolute exposures above are for that day's light only; ratios and the SNR curve hold.
- **HDR channel outputs:** ch3 photon transfer differs 9× between two runs and ch1 and ch2
  look identical, although they should differ. The 12-bit channel outputs may be scaled
  inside the camera. The merged 24-bit output is not affected.
- Temperature behaviour: not measured (operating range −20 to 55 °C ambient [manual]).

## 12. Test conditions and sources
- Bench: white board lit by two LED panels (10-step dimmers), opal acrylic diffuser on the
  lens, Kowa 8 mm f/1.4–f/16, lens cap for darks. Unit 233100505, firmware 1.138.0.0
  (device version K7A4W).
- Bench findings: `docs/experiments/20261021_findings.md`
- Camera profile: `config/camera_profiles/233100505.yaml`
- Fingerprints: `WS/config/camera_fingerprints/233100505/`
- Lucid TRI054S documentation (firmware 1.138.0.0): `Manuals/vision/lucid_triton/`
