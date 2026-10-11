# GLBMIA — Maya GLB import/export

Standalone Python plug-in, tested with Maya 2023. No pip packages or Blender required.

## Install

Download and extract the repository ZIP. Keep `install.py` and the three plug-in files together. Drag `install.py` onto Maya's viewport. The installer copies only the plug-in code into the current user's Maya modules folder, loads the plug-in, and enables Auto load. Restart Maya after installation for module discovery.

Alternatively, run the installer from Maya's Python Script Editor:

```python
from pathlib import Path
path = Path('/path/to/extracted/glbmia/install.py')
exec(compile(path.read_text(), str(path), 'exec'), {'__file__': str(path), '__name__': '__main__'})
```

Manage **Loaded** and **Auto load** under **Windows > Settings/Preferences > Plug-in Manager**, using `glbImport.py`. To uninstall, unload it and remove `glbmia.mod` and the `glbmia` folder from your Maya user modules folder. Updates use the same installer; a loaded plug-in must be able to unload before files are replaced.

## Import

Use **File > Import > glTF Binary**. Supports GLB 2.0 triangle geometry, hierarchy, transforms, normals, UV0, vertex colors, sparse/interleaved accessors, PNG/JPEG textures, PBR factors, alpha modes and unlit materials. Converts glTF metres to Maya centimetres and adapts to the scene up axis.

STEP and LINEAR transform animations become Maya animation curves. All clips receive consecutive named frame ranges; choose them under **GLB > Animation Clips...**. Quaternion LINEAR rotation is sampled, so interpolation between samples is approximate.

Textures are extracted beside the input into `<filename>_textures`; keep this folder and the source GLB available. Skins, morph targets, CUBICSPLINE and unsupported required extensions (including Draco/Meshopt) are rejected. Cameras/lights and occlusion maps are not imported. Vertex color rendering depends on the renderer.

## Export with animations

Select an imported GLB group and use **GLB > Export Imported GLB with Animations...**. This verifies the live scene against its original GLB, preserves the original geometry, normals, vertex colors, UVs, textures, hierarchy and animation clips, and applies supported live material changes such as opacity. Keep the original GLB available. Geometry, rig and animation changes that cannot be faithfully preserved cause an explicit error. Skinned scenes are rejected. This is a verified imported-asset roundtrip, not a general Maya animation or skeleton exporter.

```python
import glbImport
glbImport.export_imported_glb('/path/to/new-output.glb', root='model_GLB')
```

## Static mesh export

**File > Export Selection > glTF Binary** and **GLB > Export Selected for Pyrite64...** export static meshes, including triangulation, materials, normals, UV seams and face-vertex colors. They do not export animations or skeletons. Pyrite64 mode supports native Fast64 material properties and writes external PNG companions. Copy those images with the GLB. Generic export embeds images:

```python
import glbImport
glbImport.export_glb('/path/to/new-static.glb', selected=True, pyrite64=False)
```

Static export bakes world transforms. `Col` plus grayscale `Alpha` color sets are combined when available; otherwise the current color set is used. `UVMap` is preferred over the current UV set. Arbitrary Maya shader networks are not translated. Exporters protect existing output filenames.

Only source code and installation/documentation files are distributed; no user models, scenes, textures or machine paths are included.
