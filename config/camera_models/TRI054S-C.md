# Lucid Triton TRI054S-C — camera white paper (DRAFT v8, 2026-10-23)

What this camera is, how it works, how it behaves and how well it performs, measured on the
camera_lab bench. Machine-readable twin: `TRI054S-C.yaml`.

Tags: **[measured date]** our bench · **[manual]** Lucid documentation · **[to measure]**
the next fingerprint run fills it in. Numbers of one unit (hot pixels, exact black level)
are in that serial's fingerprint: `WS/config/camera_fingerprints/<serial>/`.

## 1. Summary
A 5.4 MP colour HDR camera built around Sony's IMX490 automotive sensor. Each pixel has two
sub-pixels read at two conversion gains; the sensor merges these four signals on the chip
into one linear 24-bit image with more than 120 dB of dynamic range, so very bright and
very dark areas are captured in a single exposure. It has a rolling shutter. Out of the
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

The linear 24-bit image has a black level of about 194 DN set inside the camera; the
`BlackLevel` control (0–12.5 %, 0 by default) does not move it — it only shifts the
single-channel outputs, by % of 4095. [measured 2026-10-22]

## 4. Controls and their behaviour
| control | behaviour |
|---|---|
| exposure | sets all four HDR channel exposures together while HDR tuning is off; 86 µs minimum; beyond one frame period it lowers the frame rate | [measured 2026-10-21/22] |
| gain | with HDR tuning on, `ExposureTime` and `Gain` become read-only and the per-channel exposures/gains apply instead | [measured 2026-10-21] |
| HDR tuning (`HDRTuningEnable`) | off: one exposure for all channels; on: per-channel exposure and gain | [measured 2026-10-21] |
| `HDROutput`, `LUTEnable`, pixel format | **must be changed with the stream stopped**: written while streaming they are silently ignored | [measured 2026-10-22] |
| user sets | Default + UserSet1/2; a user set can be the boot set; it does **not** store HDR tuning or the HDR channel values | [measured 2026-10-21] |
| power-up state | boots from its boot user set with HDR tuning **off** and channel gains at the factory values | [measured 2026-10-21] |

### Setting names (camera_lab key ↔ GenICam node)
| camera_lab | GenICam node | notes |
|---|---|---|
| `exposure_us` | `ExposureTime` | µs; read-only while HDR tuning is on |
| `gain_db` | `Gain` | dB; read-only while HDR tuning is on |
| `pixel_format` | `PixelFormat` | stream stop required |
| `frame_rate_hz` | `AcquisitionFrameRate` | needs `AcquisitionFrameRateEnable` |
| `white_balance` (red/green/blue) | `BalanceRatio` + `BalanceRatioSelector` | |
| `hdr.enabled` | `HDRTuningEnable` | |
| `hdr.output` | `HDROutput` (`HDR`, `HDRChannel0…3`) | stream stop required |
| `hdr.enhancement` | `HDRImageEnhancementEnable` | |
| `hdr.clamping` | `HDRDigitalClampingEnable` | |
| `hdr.chN.exposure_us` | `HDRChannelExposureTime` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `hdr.chN.gain_db` | `HDRChannelAnalogGain` with `HDRTuningChannelSelector = N` | HDR tuning on |
| `lut.enabled` | `LUTEnable` | stream stop required |
| `lut.tone_mapping` | `LUTToneMapping` (`Linear`, `GammaPointFive`, …) | |
| `color.transformation` | `ColorTransformationEnable` | |
| `black.level` | `BlackLevel` | % of full scale; single-channel outputs only |
| profile mode `raw_hdr` | — | BayerRG24 + `hdr.enabled` off + `hdr.output` HDR + `lut.enabled` off + `color.transformation` off |

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

## 6. Measured performance (linear 24-bit output, Kowa 8 mm f/1.4)
Full scale = 2²⁴ − 1 = 16 777 215 DN. "Runs" = how many measurement runs the value rests on.

| property | value | fraction of full scale | runs / units | source |
|---|---|---|---|---|
| black level | ≈ 194 DN | 0.0012 % | 1 run / 1 unit | [measured 2026-10-22] |
| read noise | ≈ 64 DN (60–64 DN from 0.1 to 12 ms) | 0.00038 % | 2 / 1 | [measured 2026-10-22/23] |
| row noise (temporal, dark) | ≈ 8 DN rms, independent of exposure: ≈ 1/8 of the pixel noise | | 1 / 1 | [measured 2026-10-23] |
| **dynamic range** (saturation ÷ read noise) | **≈ 108 dB (18.0 stops)** — below the datasheet's > 120 dB | | 1 / 1 | [measured 2026-10-22] |
| noise model σ² = a + b·signal | a, b **[to measure]** (predicts noise and SNR at any level) | | | |
| linearity error | 0.24 % of range | | 1 / 1 | [measured 2026-10-22] |
| SNR vs signal level | **not monotonic** — see the curve below | | 1 / 1 | [measured 2026-10-23] |
| dark current | ≈ 0 DN/s | | 1 / 1 | [measured 2026-10-22] |
| pixel response non-uniformity | 1.3 % | | 1 / 1 | [measured 2026-10-22] |
| vignetting | corners 0.90 of centre (lens- and iris-dependent) | | 1 / 1 | [measured 2026-10-22] |
| colour: R/G, B/G, green imbalance | **[to measure]** under the bench light (white LED panels): a white-balance starting point | | | |
| run-to-run spread | **[to measure]** (3 runs per measurement) | | | |
| hot pixels | unit-specific (521 on 233100505, 8 σ criterion) | | 1 / 1 | [measured 2026-10-22] |

### SNR and effective resolution vs signal level [measured 2026-10-23, one run]
| level (% of full scale) | quantization step | effective bits | temporal noise σ | SNR |
|---|---|---|---|---|
| 0.08 % | 26 DN | 19.3 | 2 156 DN | 6 |
| 0.5 % | 26 DN | 19.3 | 3 378 DN | 25 |
| 1 % | 577 DN | 14.8 | 5 315 DN | 31 |
| 4 % | 577 DN | 14.8 | 10 358 DN | 67 |
| **8 %** | 577 DN | 14.8 | 14 362 DN | **96 (best)** |
| **10–11 %** | handover | — | 62 000–86 000 DN | **21–27 (worst)** |
| 20 % | 4 157 DN | 12.0 | 108 166 DN | 31 |
| 44 % | 4 157 DN | 12.0 | 155 904 DN | 47 |
| 74 % | 4 157 DN | 12.0 | 200 208 DN | 62 |
| ≈ 100 % | saturation | | | |

- The on-chip merge hands over to less sensitive channels as the signal rises; each
  handover makes the quantization step jump (×22 near 1 %, ×7.2 near 10 %). The step is
  always ≤ 1/8 of the noise, so quantization never limits the data.
- The handover near **10 % of full scale costs a 4× SNR drop** (96 → 21) that only
  recovers to ≈ 60 near 75 %. Scenes whose important parts sit just below 9 % get the best
  SNR; parts at 10–20 % get the worst. Below ≈ 0.01 % the codes are sparse and irregular
  (0, 13, 14, 21, …).
- The 24-bit output is **not** an 18-bit signal scaled up: odd and even values are equally
  common.

## 7. The four HDR channels
| channel | sub-pixel / conversion gain | sensitivity vs ch3 | gain DN/e⁻ | full well e⁻ | read noise DN |
|---|---|---|---|---|---|
| ch0 | large, high (most sensitive) | ≈ 6200× (22× ch1, from the quantization steps) | **[to measure]** | **[to measure]** | 67 |
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
| dark | lens cap on, 10 ms | black ≈ 194 DN, read noise ≈ 64 DN |
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
