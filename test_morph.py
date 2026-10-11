"""Synthetic morph regression; run with mayapy. No external assets required."""
import json
import struct
import tempfile
from pathlib import Path
import maya.standalone
maya.standalone.initialize(name='python')
import maya.cmds as cmds
import maya.api.OpenMaya as om
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent/'plug-ins'))
import glbformaya

binary = bytearray(); views = []; accessors = []
def accessor(rows, kind):
    flat = [v for row in rows for v in row]
    blob = struct.pack('<'+'f'*len(flat), *flat)
    views.append({'buffer':0,'byteOffset':len(binary),'byteLength':len(blob)})
    binary.extend(blob)
    accessors.append({'bufferView':len(views)-1,'componentType':5126,'count':len(rows),'type':kind})
    return len(accessors)-1

position = accessor([(0,0,0),(1,0,0),(0,1,0)],'VEC3')
delta = accessor([(0,0,1)]*3,'VEC3')
times = accessor([(0,),(1,)],'SCALAR')
weights = accessor([(-1,),(2,)],'SCALAR')
primitive = {'attributes':{'POSITION':position},'targets':[{'POSITION':delta}]}
doc = {'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],
       'nodes':[{'mesh':0,'weights':[0.25]}],
       'meshes':[{'primitives':[primitive,primitive],'weights':[0.5]}],
       'animations':[{'samplers':[{'input':times,'output':weights,'interpolation':mode}],
                      'channels':[{'sampler':0,'target':{'node':0,'path':'weights'}}]} for mode in ('LINEAR','STEP')],
       'bufferViews':views,'accessors':accessors,'buffers':[{'byteLength':len(binary)}]}
payload = json.dumps(doc).encode(); payload += b' '*(-len(payload)%4)
binary += b'\0'*(-len(binary)%4)
data = struct.pack('<4sII',b'glTF',2,28+len(payload)+len(binary))+struct.pack('<II',len(payload),0x4e4f534a)+payload+struct.pack('<II',len(binary),0x004e4942)+binary
with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary)/'morph.glb'; path.write_bytes(data)
    root = glbformaya.import_glb(str(path))
    assert len(cmds.ls(type='blendShape')) == 2
    fps = om.MTime(1,om.MTime.kSeconds).asUnits(om.MTime.uiUnit())
    clips = glbformaya.animation_clips(root)
    for frame, expected in ((1,-1),(1+fps/2,0.5),(1+fps,2),(clips[1]['start']+fps/2,-1)):
        cmds.currentTime(frame)
        for shape in cmds.ls(root,dag=True,type='mesh',long=True):
            if cmds.getAttr(shape+'.intermediateObject'): continue
            selection = om.MSelectionList(); selection.add(shape)
            points = om.MFnMesh(selection.getDagPath(0)).getPoints()
            assert all(abs(p.z-expected)<1e-5 for p in points), (frame,expected,points)
    try:
        glbformaya.export_imported_glb(str(Path(temporary)/'out.glb'),root)
    except ValueError as error:
        assert 'morph' in str(error)
    else:
        raise AssertionError('Morph roundtrip must refuse unsupported export')
print('PASS: multi-primitive morphs, negative and >1 weights, LINEAR/STEP clips, export guard')
