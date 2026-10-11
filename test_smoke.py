"""Run with Maya 2023 mayapy; uses only a generated cube in an isolated scene."""
import json
import sys
import tempfile
from pathlib import Path
import maya.standalone
maya.standalone.initialize(name='python')
import maya.cmds as cmds
import install

with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary)
    plugins = install.install(folder / 'modules', load=False)
    assert all((plugins / name).is_file() for name in install.FILES)
    assert (folder / 'modules' / 'glbmia.mod').is_file()
    sys.path.insert(0, str(plugins))
    cmds.loadPlugin(str(plugins / 'glbImport.py'))
    assert 'glTF Binary' in cmds.pluginInfo('glbImport.py', query=True, translator=True)
    import glbImport
    cube = cmds.polyCube()[0]
    cmds.select(cube)
    output = folder / 'cube.glb'
    glbImport.export_glb(str(output), selected=True, pyrite64=False)
    source = glbImport.GLB(str(output))
    assert len(source.doc['meshes']) == 1
    cmds.file(new=True, force=True)
    root = glbImport.import_glb(str(output))
    meshes = cmds.ls(root, dag=True, type='mesh', long=True)
    assert sum(cmds.polyEvaluate(mesh, face=True) for mesh in meshes) == 12
    roundtrip = folder / 'roundtrip.glb'
    result = glbImport.export_imported_glb(str(roundtrip), root)
    exported = glbImport.GLB(str(roundtrip))
    assert exported.buffers == source.buffers
    assert exported.doc['meshes'] == source.doc['meshes']
    try:
        glbImport.export_imported_glb(str(roundtrip), root)
    except ValueError:
        pass
    else:
        raise AssertionError('Existing output must be protected')
    cmds.file(new=True, force=True)
    cmds.unloadPlugin('glbImport.py')
    print('PASS: installer, plug-in lifecycle, static export, import, roundtrip and overwrite protection')
