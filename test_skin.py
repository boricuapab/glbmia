"""Synthetic joints, inverse bind matrices, UV2 and skin-animation regression."""
import json
import struct
import sys
import tempfile
from pathlib import Path
import maya.standalone
maya.standalone.initialize(name='python')
import maya.cmds as cmds
import maya.api.OpenMaya as om
sys.path.insert(0,str(Path(__file__).resolve().parent/'plug-ins'))
import glbformaya
from PySide2.QtGui import QImage

binary=bytearray();views=[];accessors=[]
def accessor(rows,kind,component=5126):
    values=[v for row in rows for v in row]
    blob=struct.pack('<'+('f' if component==5126 else 'H')*len(values),*values)
    binary.extend(b'\0'*(-len(binary)%4))
    views.append({'buffer':0,'byteOffset':len(binary),'byteLength':len(blob)})
    binary.extend(blob)
    accessors.append({'bufferView':len(views)-1,'componentType':component,'count':len(rows),'type':kind})
    return len(accessors)-1
position=accessor([(0,0,0),(1,0,0),(0,1,0)],'VEC3')
uv=accessor([(0,0),(1,0),(0,1)],'VEC2')
joints=accessor([(0,1,0,0)]*3,'VEC4',5123)
weights=accessor([(0.5,0.5,0,0)]*3,'VEC4')
matrices=[]
for x in (-1,-2):
    matrix=list(om.MMatrix());matrix[12]=x;matrices.append(matrix)
bind=accessor(matrices,'MAT4')
times=accessor([(0,),(0.50000001,),(1,)],'SCALAR')
translations=accessor([(1,0,0),(1.5,0,0),(2,0,0)],'VEC3')
rotations=accessor([(0,0,0,1)]*3,'VEC4')
doc={'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],
     'nodes':[{'children':[1,3]},{'name':'jointA','translation':[1,0,0],'children':[2]},
              {'name':'jointB','translation':[1,0,0]},{'name':'mesh','mesh':0,'skin':0,'translation':[3,0,0]}],
     'skins':[{'joints':[1,2],'inverseBindMatrices':bind}],
     'meshes':[{'primitives':[{'attributes':{'POSITION':position,'TEXCOORD_0':uv,'TEXCOORD_2':uv,'JOINTS_0':joints,'WEIGHTS_0':weights},'material':0}]}],
     'images':[{'uri':'texture.png'}],'textures':[{'source':0}],
     'materials':[{'pbrMetallicRoughness':{'baseColorTexture':{'index':0,'texCoord':2}}}],
     'animations':[{'samplers':[{'input':times,'output':translations},{'input':times,'output':rotations}],
                    'channels':[{'sampler':0,'target':{'node':2,'path':'translation'}},{'sampler':1,'target':{'node':2,'path':'rotation'}}]}],
     'bufferViews':views,'accessors':accessors,'buffers':[{'byteLength':len(binary)}]}
payload=json.dumps(doc).encode();payload+=b' '*(-len(payload)%4);binary+=b'\0'*(-len(binary)%4)
data=struct.pack('<4sII',b'glTF',2,28+len(payload)+len(binary))+struct.pack('<II',len(payload),0x4e4f534a)+payload+struct.pack('<II',len(binary),0x004e4942)+binary
with tempfile.TemporaryDirectory() as temporary:
    folder=Path(temporary); path=folder/'skin.glb';path.write_bytes(data)
    image=QImage(2,2,QImage.Format_RGBA8888);image.fill(0xffffffff);assert image.save(str(folder/'texture.png'))
    root=glbformaya.import_glb(str(path))
    assert len(cmds.ls(type='joint'))==2 and len(cmds.ls(type='skinCluster'))==1
    shape=next(m for m in cmds.ls(root,dag=True,type='mesh',long=True) if not cmds.getAttr(m+'.intermediateObject'))
    selection=om.MSelectionList();selection.add(shape);fn=om.MFnMesh(selection.getDagPath(0))
    assert 'glbUV2' in fn.getUVSetNames()
    texture=cmds.ls(type='file')[0]
    assert any('uvSet[1]' in item for item in cmds.uvLink(query=True,texture=texture)),cmds.uvLink(query=True,texture=texture)
    fps=om.MTime(1,om.MTime.kSeconds).asUnits(om.MTime.uiUnit())
    for time,x in ((1,0),(1+fps/2,25),(1+fps,50)):
        cmds.currentTime(time)
        assert abs(fn.getPoints(om.MSpace.kWorld)[0].x-x)<1e-4, (time,x,fn.getPoints(om.MSpace.kWorld)[0].x)
    scene=folder/'skin.mb';cmds.file(rename=str(scene));cmds.file(save=True,type='mayaBinary')
    cmds.file(new=True,force=True);cmds.file(str(scene),open=True,force=True)
    assert len(cmds.ls(type='skinCluster'))==1
print('PASS: joint hierarchy, blend weights, inverse binds, mesh transform, UV2, near-frame timestamps, save/reopen')
