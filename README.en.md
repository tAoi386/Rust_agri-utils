# Rust Growing Toolkit

**V3.0.0 · Windows desktop application**

[简体中文](README.md) · [English](README.en.md) · [Русский](README.ru.md)

Plan breeding routes in Rust from the six-letter genes and actual quantities of the clones you already own. The toolkit provides sequential breeding, parallel planters, screen recognition and automatically saved local projects. Author: **Tao** · [2027883286@qq.com](mailto:2027883286@qq.com).

## Getting started

Repository: [tAoi386/Rust_agri-utils](https://github.com/tAoi386/Rust_agri-utils). **Code → Download ZIP** on GitHub downloads the source. Extract it into `Rust_agri-utils`, prepare Python as described below and run `launch.bat`. Alternatively, clone the repository:

```powershell
git clone https://github.com/tAoi386/Rust_agri-utils.git
cd Rust_agri-utils
.\launch.bat
```

If a portable build already exists locally, run **`启动种养殖工具.bat`**; the previous `启动苗圃.bat` launcher remains compatible. Within the source tree, the executable lives at `dist\capture\RustSeedLab\RustSeedLab.exe`. If you have a portable archive, extract it and run the included `RustSeedLab.exe` directly. Copy the entire `RustSeedLab` folder when moving it to another computer. The portable build does not require Python.

The interface supports **Simplified Chinese, English and Russian**. Use the application's New Project and Open Project actions to manage projects. Changes save automatically; there is no need to choose a save folder. Save Now writes the current work immediately.

Each project contains one crop. Enter known six-letter genes, names and the number of clones actually available. Grow unknown seeds in the game before recording their genes. Seed numbers remain consistent across both breeding pages. The default goal is **3G3Y in any order**: both `GGYYYG` and `YGYGYG` qualify. Exact gene order is also available.

## Two breeding modes

### Sequential breeding

1. Enter or recognize your gene library. Set the target, planning depth and expected number of clones produced per round.
2. Choose a preference: fewer rounds, fewer total plants, or **Smart matching (based on your gene library)**. Automatic mode compares recipients and donors together; you can also pin a recipient.
3. Start automatic selection. The first planting layout appears only after a complete route to the target has been found. A failed search reports its limits and shows a brief floating notice.
4. Follow the layout: plant the recipient in central slot 5 first. Add the specified donors as the recipient approaches crossbreeding; leave unused slots empty. The central cell identifies the seed number, its current genes and the predicted result.
5. After crossbreeding and taking clones, enter the observed six-letter genes and the actual clone quantity. Confirm the round after verification. The application deducts its scheduled plants, adds the observed clones and replans from the actual remaining stock.

A prediction is not an acquired plant. **Observed genes must match the prediction position by position before the next round can be confirmed.** An any-order final goal does not relax this verification. If the observation differs, check the growth stage, plants and layout; no stock is changed. A clone already meeting the goal can be kept directly without further crossbreeding.

### Parallel breeding

Choose **2–6 planters** and designate the main planter. The default is three planters with planter 1 as the main one. Smart planning can prepare future recipients or donors in auxiliary planters while advancing the target route in the main planter. Each planter has its own layout, prediction, observation and clone quantity. Supply notes identify the later batch and planter that need each output.

All planters in a batch share the same real inventory. One plant cannot be allocated twice, and an output that has not yet been grown cannot be used elsewhere in the same batch. **Every active planter must pass observation checks before the whole batch is confirmed atomically.** The next batch is then replanned using actual outputs. Unneeded planters stay empty.

**Keep planters far enough apart that plants in one planter do not enter another recipient's crossbreeding influence range.** This reminder stays visible. Planter numbers and corner or edge positions do not add gene voting weight. Check actual spacing and growth stages in the game.

Parallel planters can prepare intermediate clones together, but three planters do not guarantee a two-batch solution. Estimated batches describe dependencies, not elapsed minutes or the shortest possible growth time. Feasibility depends on the genes, quantities, expected clone yield and search scope.

## Recognition and inventory

Read Genes from Game processes the gene information currently visible on the screen; it does not require a recorded video. The main window hides during capture and returns when capture ends. The default view looks for the item details near the screen center. You can adjust the region or import an image. Show all six genes, wait for confirmation, move to the next plant, then review the recognized list before importing it.

Recognition uses the local Windows OCR components on Windows 10/11. It does not read game memory or automate in-game actions. Blur, occlusion and background effects can cause mistakes, so review the results. Manual and bulk entry remain available when OCR is unavailable.

Deduplication uses the **exact ordered six-letter genotype**. Repeated scans do not increase stock. If you obtain more clones with an existing genotype, edit its available quantity. Quantities represent real plants available for future assignments, not the number of sightings of a genotype.

## Saving and undo

Projects live in `data/projects/` beside the program; `data/autosave.json` stores the latest workspace. Saved data includes inventory, seed numbers, target, mode, planter configuration, pending assignments, observation drafts and completed history. Put the portable application in a writable directory, and include `data/` when backing up or moving your work.

Legacy JSON projects placed beside the program are imported into the internal project list at startup; the original files are retained. Keep demonstration projects separate from real inventories so example quantities are not mistaken for plants you own.

Previewing a plan consumes nothing. Confirmation records actual use. You can undo the latest sequential round or parallel batch, restoring its inputs and removing its added clones from the software inventory. Undo changes software records only. If stock has since changed, the application prevents an undo that would overwrite those changes. Unconfirmed assignments can also be cancelled. A pending assignment locks the other breeding mode and inventory editing to prevent double allocation.

Logs remain local in `data/logs/runtime.log` and `data/logs/native-crash.log`. If that directory is not writable, the application attempts `RustSeedLab/logs` in the system temporary directory. Logs are not automatically uploaded.

## Anonymous example

[examples/parallel.json](examples/parallel.json) contains a three-planter example with no personal data or pending work. Alternatively, create a project and paste these rows into bulk entry, with one clone per row and the any-order 3G3Y goal:

```text
HGYGHG 1 Demo01
HGGGYY 1 Demo02
HYYGYG 1 Demo03
YHYYHH 1 Demo04
GYHYYY 1 Demo05
YYHYYY 1 Demo06
YHYYHG 1 Demo07
```

The model permits a two-batch fork and merge for this library: two planters prepare intermediate clones, then the main planter combines them to obtain `GGYYYG`. This demonstrates shared-stock accounting and supply dependencies. Search may return another complete route, and actual game results must still be entered after each batch.

## Planning limits

- `G` means growth, `Y` yield, `H` hardiness, `W` water consumption and `X` an empty gene. An empty planting slot is not a plant carrying `X` genes.
- The recipient occupies the center of a nine-slot planter, with at most eight donors. The model compares matching gene positions. Layouts guide placement; moving the same donors between eligible corners or edges does not change their modeled votes.
- Search is bounded by time, combinations and candidate coverage. No Route Found does not prove that the library is insufficient or the target unreachable. A good route found within the budget is not a proof of global optimality.
- Planning uses deterministic intermediate outcomes and verifies stock batch by batch. Expected clone yield is a planning assumption; confirmation always uses the actual quantity obtained.
- The toolkit does not plant, remove plants or take clones for you. Its model and tests do not replace observations in the game. Game versions, server rules, distances and timing can affect results. This is not an official Facepunch product.

## Running from source and building

Use Python **3.10+**. Run `launch.bat` to start from source. Missing dependencies are installed into the project's `.deps/` directory; the first installation needs network access. Alternatively, run these commands from the repository root:

```powershell
python -m pip install --target .deps -r requirements.txt
$env:PYTHONPATH = "$PWD;$PWD\.deps"
python main.py
```

Run the checks:

```powershell
python -m unittest discover -s tests -v
```

Run `build.bat` to create a portable build. Build dependencies use `.build-deps/`; staging uses `dist/build-new/`; the active application lives in `dist/capture/RustSeedLab/`. Close the running application before updating. The update preserves `data/` and backs up the previous portable folder under `artifacts/portable-build-backups/`.

Before packaging, run the built executable with `--smoke-test` and verify that the current version passes. Then run `python tools/package_release.py`. Release archives exclude personal inventories, autosaves and logs, and include dependency notices under `third-party-notices/`. The repository's `.gitignore` excludes local data, build output and caches. Do not add personal projects or screenshots containing private information to a public repository.
