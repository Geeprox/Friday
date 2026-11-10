# Real clothing mesh sources

The three clothing items are actual MakeHuman Community garment meshes, licensed
CC0 1.0. No texture, brand, or paid marketplace asset is used. The selected file
headers and official per-asset pack tables agree on CC0. The full CC0 text is in
`MAKEHUMAN-LICENSE.ASSETS.md` beside this notice.

| Item | Exact official asset | Author / copyright holders | Official source table |
| --- | --- | --- | --- |
| Short sleeve T-shirt | `elvs_crude_t-shirt_male` / `crude_male_shirt.obj` | Elvaerwyn; file header: Makehuman edited by Elvaerwyn | https://static.makehumancommunity.org/assets/assetpacks/shirts01.html |
| Loose long trousers | `toigo_wool_pants` / `pants_wool.obj` | MargaretToigo; file header: MRT | https://static.makehumancommunity.org/assets/assetpacks/pants01.html |
| Low lace-up sneakers | System asset `shoes02` | Copyright 2020 Data Collection AB, Joel Palmius, Jonas Hauquier | https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html |

Exact retained OBJ/MHCLO/MHW files, official-page snapshots, archive URLs,
timestamps, entry digests, and full-archive digests where downloaded are in
`.artifacts/design-20261103/body-source-review/upstream-garments/SOURCE-MANIFEST.json`
(relative to the repository root) and its referenced pack receipts.
The 267 MB system pack was read using verified HTTP byte ranges; only the
selected sneaker entries were downloaded. A whole-archive digest is not claimed.

Friday adaptations: fit each garment using its original MHCLO barycentric
surface mapping and offset, transfer the existing 15-bone body weights through
that mapping, use the official sneaker-specific MHW weights, retain garment
topology, render smooth white/light neutral fills, derive thin graphite strokes
from exact collar/cuff/hem opening edges, and hide covered anatomical base faces.
The existing continuous base positions, original rig and all thirteen clips are
retained. The three explicitly Friday-authored motions remain identified as such.

The source garment geometry is distinct from the body geometry. Clothing is not
a recolored or expanded nude body. The standalone approved Godot hand assets are
outside this candidate and were not read or changed.
