# glbformaya — Maya GLB import/export

Standalone Python plug-in for Maya. No pip packages or Blender required.

## Install

Download and extract the repository ZIP. Keep the extracted folder structure intact. Only `plug-ins/glbformaya.py` is loadable; internal helpers live in `scripts/glbformaya_support`. Drag `install.py` onto Maya's viewport. The installer copies only the plug-in code into the current user's Maya modules folder, loads the plug-in, and enables Auto load. Restart Maya after installation for module discovery.

Alternatively, run the installer from Maya's Python Script Editor:

```python
from pathlib import Path
path = Path('/path/to/extracted/glbformaya/install.py')
exec(compile(path.read_text(), str(path), 'exec'), {'__file__': str(path), '__name__': '__main__'})
```

Manage **Loaded** and **Auto load** under **Windows > Settings/Preferences > Plug-in Manager**, using `glbformaya.py`. To uninstall, unload it and remove `glbformaya.mod` and the `glbformaya` folder from your Maya user modules folder. The installer migrates recognized older copies and removes their extra loader entries. Updates use the same installer; a loaded plug-in must be able to unload before files are replaced.

## Import

Use **File > Import > glTF Binary**. Supports GLB 2.0 triangle geometry, hierarchy, transforms, normals, multiple UV sets, vertex colors, sparse/interleaved accessors, PNG/JPEG textures, PBR factors, alpha modes and unlit materials. Converts glTF metres to Maya centimetres and adapts to the scene up axis.

STEP and LINEAR transform animations become Maya animation curves. All clips receive consecutive named frame ranges; choose them under **glbformaya > Animation Clips...**. Quaternion LINEAR rotation is sampled, so interpolation between samples is approximate.

Textures are extracted beside the input into `<filename>_textures`; keep this folder and the source GLB available. CUBICSPLINE and unsupported required extensions (including Draco/Meshopt) are rejected. Cameras/lights and occlusion maps are not imported. Vertex color rendering depends on the renderer.

Morph POSITION targets and STEP/LINEAR weight animations import as native Maya blend shapes. Node weights override mesh defaults. Supplied morph NORMAL deltas are validated but normals are recalculated from the deformed geometry; tangent and other morph attributes are rejected.

Skinned meshes import as native Maya joints and skinClusters, preserving inverse bind matrices and up to four influences per vertex. Joint scale inheritance follows glTF. Materials are linked to their requested UV sets. Animation timestamps that coincide at Maya tick resolution are merged before curves are created.

## Export with animations

Select an imported GLB group and use **glbformaya > Export Imported GLB with Animations...**. This verifies the live scene against its original GLB, preserves the original geometry, normals, vertex colors, UVs, textures, hierarchy and animation clips, and applies supported live material changes such as opacity. Keep the original GLB available. Geometry, rig and animation changes that cannot be faithfully preserved cause an explicit error. Skinned scenes are rejected. Morph POSITION targets and STEP/LINEAR weight animation can be preserved after verifying native blend shapes and their animation against the source. Original NORMAL deltas are retained in the GLB. Transform animation verification currently supports STEP clips. This is a verified imported-asset roundtrip, not a general Maya animation or skeleton exporter.

```python
import glbformaya
glbformaya.export_imported_glb('/path/to/new-output.glb', root='model_GLB')
```

## Static mesh export

**File > Export Selection > glTF Binary** and **glbformaya > Export Selected for Pyrite64...** export static meshes, including triangulation, materials, normals, UV seams and face-vertex colors. When selecting an entire imported GLB group, File > Export Selection automatically uses the verified animation-preserving roundtrip. The explicit Pyrite64 menu remains static; ordinary Maya mesh export does not export animations or skeletons. Pyrite64 mode supports native Fast64 material properties and writes external PNG companions. Copy those images with the GLB. Generic export embeds images:

```python
import glbformaya
glbformaya.export_glb('/path/to/new-static.glb', selected=True, pyrite64=False)
```

Static export bakes world transforms. `Col` plus grayscale `Alpha` color sets are combined when available; otherwise the current color set is used. `UVMap` is preferred over the current UV set. Arbitrary Maya shader networks are not translated. Exporters protect existing output filenames.

Only source code and installation/documentation files are distributed; no user models, scenes, textures or machine paths are included.
