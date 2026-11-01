# Controlled bench guidance

How to set up the bench so camera_lab's numbers can be trusted and repeated (Phase 10 step 6).
Every rule below says why, and most of them come from something that went wrong on this bench;
the evidence is in `docs/experiments/` and in `CONTEXT.md`.

**What a controlled bench is for.** A number is only useful when the next measurement can be
compared with it: this camera against that one, today against last week, before against after a
lens change. Everything that is not the camera (stray light, light that drifts or flickers,
a distance that moved, a print that bows) adds to the difference and gets blamed on the camera.

## 1. Blackout enclosure

- Close the bench in black, matte, opaque cloth or board on all sides and the top, with the
  camera looking in through one opening. Daylight from a window and room lights change during
  the day; ceiling lights on mains also flicker at 120 Hz (60 Hz mains).
- Black inside, matte: shiny surfaces (a white wall, a metal frame, the robot) reflect the bench
  light back onto the chart and into the lens.
- **Check:** bench lights off, enclosure closed, a capture at the working exposure should be as
  dark as the lens cap. Anything brighter is light getting in.
- Keep the enclosure closed for every measurement in a series, including the fingerprint rows
  (the cap and diffuser rows assume no other light).

## 2. Steady LED light

- **Use the DC LED floods at full on the opal acrylic diffuser** (the bench light since
  2026-10-23, `config/fingerprint_bench.yaml`). Not the dimmed LED panels: dimming makes them
  flicker at 120 Hz, 1.8–2.8 % row bands at 0.5–4 ms on a rolling shutter (2026-10-23).
- **Keep exposures long enough for the floods' ripple** (≈ 4 kHz): ≥ 2 ms on rolling-shutter
  cameras (TRI054S-C), ≥ 0.5 ms on global-shutter cameras (TRI089S-M, AP-3200T). Shorter, the
  ripple shows as bands or frame-to-frame changes. Set the brightness with the iris or the
  number of floods, not with a very short exposure.
- **Light from the sides at about 45°**, two floods mirrored, so the chart is evenly lit and the
  lamps' reflections from the paper do not land in the lens. On 2026-10-31 the mono's light check
  passed but the 3 × 3 brightness was strongly uneven: aim the floods before measuring corners.
- Let the lights and the camera warm up before absolute numbers (the TRI089S-M ran at 50 °C on
  2026-10-31). How long this bench needs has not been measured yet; the light check reports a
  drift between its start and end (warns above 5 %).
- **Check:** `camera-lab two-d assess-light <camera>` (or MCP `assess_site_light`): linear output,
  flicker (row bands and frame-to-frame change, warns above 1 %), enough light for the best SNR,
  clipping, the light spread over a 3 × 3 grid. It says what to change.

## 3. Fixed mounting at the working distance

- **Camera on a rigid mount, chart on a fixed plate with stops** at the working distance, so a
  chart can come off and go back to the same place. Measure and write down the distance from the
  chart to the lens front.
- *Why:* on 2026-10-28 a corner test moved the resolution sheet around the view and changed the
  distance by accident (65 → 27 cm). Every position needed a refocus, and close up the lens is
  worse (MTF50 0.095 at ~23 cm vs 0.225 at ~65 cm, f/4). That is why the field chart exists:
  one image, one distance, one focus.
- **Chart square to the camera:** a tilted chart is sharper on one side and is read as a tilted
  lens. The field chart's boards stand against one straight rail, the same distance within
  ~5 mm (`camera-lab charts field-plan` gives the distance and the board spacing).
- **Lock the focus and iris rings** once set (lock screws or tape) and note the f-stop. Iris rings
  are hard to set between marks (the Kowa's f/5.6 and f/11 are dots): use printed marks or an end
  stop. A bumped ring shows up as a focus drift in the next setup (compared with the baseline in
  `WS/config/camera_setup/<serial>.json`).
- After moving anything (camera, lens, chart plate), run the setup's focus step again.

## 4. Matte prints, flat

- **Matte paper, no gloss or satin:** gloss sends the lamps' reflections into the lens as
  bright patches that clip.
- **Print at 100 %** (no "fit to page") and measure the 50 mm scale bar: 50 mm ± 0.5 mm, or the
  printer scaled it and lp/mm values are off.
- **Mount flat with spray adhesive** on foam board or MDF, not tape: paper that bows moves edges
  in and out of focus.
- Keep prints clean and out of the sun; a faded or dirty print is a new chart. A reprint or a
  changed layout gets a new chart id (`config/charts/`), so old results stay traceable.
- Write the print date, paper and printer in the chart's notes.

## 5. Relative tests vs absolute values

Office prints are fine for **relative** tests: camera A vs camera B, today vs last week, f/4 vs
f/5.6, centre vs corners. They are not references for **absolute** values:

| You want | Office print? | Use instead |
|---|---|---|
| Sharpness (MTF50), focus, centre vs corners | yes | the resolution and field charts |
| Exposure level, light spread, linearity | yes (relative) | grey patch, step wedge |
| Dynamic range | no: a print resolves only ~15 wedge steps, the paper's own range | the fingerprint (cap and diffuser rows) |
| Noise | no: spatial σ on a print is paper texture and light gradient | temporal noise (two frames differenced), the fingerprint |
| Absolute grey level, white balance | no | a bought 18 % grey card |
| Colour accuracy | no | a bought colour checker |

Bought references join the chart library through a printed tag card placed next to them
(`tag-card-letter-v1`), so captures record which one was in view. None is bought yet.

## 6. Before a measurement session

1. Enclosure closed, floods on (aimed, warmed up), lens rings locked, chart on its stops.
2. Only one program holds the camera: close the GUI before using the CLI or an agent, or the
   other way round (a busy camera gives exit code 3).
3. `camera-lab two-d reset <camera>`: factory Default, design mode, every setting read back.
   Never trust the state a camera booted in (the TRI089S-M boots into auto exposure).
4. `camera-lab two-d verify <camera>`: the white paper's state checks, in-camera sharpening off.
5. `camera-lab two-d assess-light <camera>`: fix what it reports before measuring.
6. Focus: GUI "Focus..." or the setup's focus step; compare with the baseline.
7. Write down what is not in the run record: distance, f-stop, which floods, enclosure state.

## 7. Real parts (not charts)

Parts on the robot cell are not matte prints: the bayonet (machined steel) is specular. Draw the
part area on the live view (GUI "Part area") so brightness, the light check and the tuners judge
the part and not the background. Light the part as it will be lit in the cell; the bench rules
above still apply to everything around it.
