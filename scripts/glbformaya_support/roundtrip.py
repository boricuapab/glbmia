"""Lossless export of verified GLB imports with live Maya material changes.

This intentionally refuses geometry/rig/animation edits it cannot reproduce.
It preserves source accessors, clips, hierarchy, textures
rather than baking a single pose. No static exporter behavior is changed.
"""
import hashlib
import json
import math
import os
import struct
import tempfile
import re

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma

from glbformaya import GLB, _name


def _close(actual, expected, tolerance=2e-5):
    return len(actual) == len(expected) and all(abs(a-b) <= tolerance for a,b in zip(actual, expected))


def _quaternion_close(a, b):
    a = a.normal(); b = b.normal()
    return abs(abs(a.x*b.x + a.y*b.y + a.z*b.z + a.w*b.w) - 1) < 1e-7


def _fn(mesh):
    selection = om.MSelectionList(); selection.add(mesh)
    return om.MFnMesh(selection.getDagPath(0))


def _curve(node, attribute):
    connections = cmds.listConnections(node+'.'+attribute, s=True, d=False) or []
    if len(connections) != 1 or not cmds.nodeType(connections[0]).startswith('animCurve'):
        raise ValueError('Cannot preserve nonstandard animation input: '+node+'.'+attribute)
    selection = om.MSelectionList(); selection.add(connections[0])
    return oma.MFnAnimCurve(selection.getDependNode(0))


def export_imported_glb(path, root=None):
    """Verify the imported scene then export its original data plus live materials."""
    if root is None:
        roots = [n for n in cmds.ls(type='transform', long=True) if cmds.attributeQuery('glbSource', node=n, exists=True)]
        if len(roots) != 1:
            raise ValueError('Specify one imported GLB root to export')
        root = roots[0]
    root = cmds.ls(root, long=True)[0]
    if not cmds.attributeQuery('glbSource', node=root, exists=True):
        raise ValueError('The export root must be a GLB import group')
    source_path = cmds.getAttr(root+'.glbSource')
    source = GLB(source_path)
    doc = json.loads(json.dumps(source.doc))
    if len(source.buffers) != 1:
        raise ValueError('Round-trip export requires a single GLB buffer')
    if doc.get('skins') or cmds.ls(root, dag=True, type='joint') or any(cmds.ls(cmds.listHistory(m) or [], type='skinCluster') for m in cmds.ls(root, dag=True, type='mesh')):
        raise ValueError('This imported-scene export has not verified skin weights; refusing to drop a skeleton')
    # Animated transforms are
    # preserved exactly as glTF nodes, including their full hierarchy.
    nodes = doc.get('nodes', [])
    node_map = {}
    shader_map = {}
    checks = {'meshes': 0, 'vertices': 0, 'triangles': 0, 'animation_samples': 0, 'normals': 0, 'colors': 0, 'uvs': 0}
    root_matrix = om.MTransformationMatrix()
    root_matrix.setScale((100,100,100), om.MSpace.kTransform)
    if cmds.upAxis(q=True, axis=True) == 'z':
        root_matrix.setRotation(om.MEulerRotation(math.pi/2,0,0))
    if not _close(cmds.xform(root, q=True, worldSpace=True, matrix=True), list(root_matrix.asMatrix())):
        raise ValueError('Import group placement/scale changed; source-preserving export cannot ignore that edit')
    animated = {(c['target']['node'], c['target']['path']) for a in doc.get('animations', []) for c in a['channels']}

    def visit(index, parent):
        node = nodes[index]
        candidates = [n for n in cmds.listRelatives(parent, children=True, fullPath=True, type='transform') or [] if n.split('|')[-1] == _name(node.get('name', 'glbNode_%d' % index))]
        if len(candidates) != 1:
            raise ValueError('Imported node renamed/missing: '+node.get('name', str(index)))
        transform = candidates[0]; node_map[index] = transform
        children = cmds.listRelatives(transform, children=True, fullPath=True, type='transform') or []
        if len(children) != len(node.get('children', [])):
            raise ValueError('Imported hierarchy changed: '+transform)
        for attr in ('rotateAxis', 'rotatePivot', 'scalePivot', 'rotatePivotTranslate', 'scalePivotTranslate', 'shear'):
            if not _close(cmds.getAttr(transform+'.'+attr)[0], [0,0,0]):
                raise ValueError('Additional Maya pivot/orientation transform: '+transform)
        if cmds.getAttr(transform+'.rotateOrder') != 0:
            raise ValueError('Changed Euler rotation order: '+transform)
        selection = om.MSelectionList(); selection.add(transform)
        local = om.MFnTransform(selection.getDependNode(0)).transformation()
        if 'matrix' in node:
            if not _close(list(local.asMatrix()), node['matrix']):
                raise ValueError('Static matrix changed: '+transform)
        else:
            if (index,'translation') not in animated and not _close(tuple(local.translation(om.MSpace.kTransform)), node.get('translation',[0,0,0])):
                raise ValueError('Static translation changed: '+transform)
            if (index,'scale') not in animated and not _close(local.scale(om.MSpace.kTransform), node.get('scale',[1,1,1])):
                raise ValueError('Static scale changed: '+transform)
            if (index,'rotation') not in animated and not _quaternion_close(local.rotation(asQuaternion=True), om.MQuaternion(*node.get('rotation',[0,0,0,1]))):
                raise ValueError('Static rotation changed: '+transform)
        shapes = cmds.listRelatives(transform, shapes=True, fullPath=True, noIntermediate=True, type='mesh') or []
        primitives = doc['meshes'][node['mesh']]['primitives'] if 'mesh' in node else []
        if len(shapes) != len(primitives):
            raise ValueError('Imported mesh primitive count changed: '+transform)
        # Importer names shapes with a deterministic per-transform numeric suffix.
        prefix = _name(node.get('name','glbMesh'))+'Shape'
        shapes.sort(key=lambda n: 0 if n.split('|')[-1] == prefix else int(n.split('|')[-1][len(prefix):]))
        for shape, primitive in zip(shapes, primitives):
            fn = _fn(shape); attrs = primitive['attributes']
            positions = source.accessor(attrs['POSITION'])
            morph = primitive.get('targets', [])
            if morph:
                blends = cmds.ls(cmds.listHistory(shape) or [], type='blendShape')
                # Follow the blend's actual input rather than relying on intermediate names.
                if len(blends) != 1:
                    raise ValueError('Morph deformer stack changed: '+shape)
                blend = blends[0]
                inputs = cmds.listConnections(blend+'.input[0].inputGeometry',s=True,d=False,shapes=True) or []
                if len(inputs) != 1 or cmds.nodeType(inputs[0]) != 'mesh':
                    raise ValueError('Morph base geometry changed: '+shape)
                base_fn = _fn(inputs[0])
                if not _close([cmds.getAttr(blend+'.envelope')],[1]):
                    raise ValueError('Morph envelope changed')
                if len(cmds.getAttr(blend+'.weight',multiIndices=True) or []) != len(morph):
                    raise ValueError('Morph target count changed')
                for wi,target in enumerate(morph):
                    incoming = cmds.listConnections(blend+'.weight[%d]' % wi,s=True,d=False,plugs=True) or []
                    if incoming != [transform+'.glbMorphWeight%d' % wi] and incoming != [transform.split('|')[-1]+'.glbMorphWeight%d' % wi]:
                        raise ValueError('Morph weight driver changed')
                    prefix = blend+'.inputTarget[0].inputTargetGroup[%d].inputTargetItem[6000]' % wi
                    if cmds.listConnections(prefix+'.inputGeomTarget',s=True,d=False):
                        raise ValueError('Live morph target edits are not supported')
                    points = cmds.getAttr(prefix+'.inputPointsTarget') or []
                    components = cmds.getAttr(prefix+'.inputComponentsTarget') or []
                    ids = []
                    for component in components:
                        match = re.fullmatch(r'vtx\[(\d+)(?::(\d+))?\]',component)
                        if not match: raise ValueError('Unsupported morph component')
                        ids.extend(range(int(match[1]),int(match[2] or match[1])+1))
                    if len(ids) != len(points): raise ValueError('Invalid morph target storage')
                    actual = {i:tuple(p)[:3] for i,p in zip(ids,points)}
                    expected = source.accessor(target['POSITION']) if 'POSITION' in target else [(0,0,0)]*len(positions)
                    if any(not _close(actual.get(i,(0,0,0)),delta) for i,delta in enumerate(expected)):
                        raise ValueError('Morph target edited: '+shape)
                fn_geometry = base_fn
            else:
                fn_geometry = fn
            if fn_geometry.numVertices != len(positions) or any(not _close(tuple(p)[:3], v) for p,v in zip(fn_geometry.getPoints(), positions)):
                raise ValueError('Geometry edited: '+shape)
            indices = [r[0] for r in source.accessor(primitive['indices'])] if 'indices' in primitive else list(range(len(positions)))
            if primitive.get('mode',4) != 4:
                raise ValueError('Verification currently supports source triangle primitives only')
            counts, vertex_ids = fn.getVertices()
            if any(c != 3 for c in counts) or list(vertex_ids) != indices:
                raise ValueError('Mesh topology edited: '+shape)
            checks['meshes'] += 1; checks['vertices'] += fn.numVertices; checks['triangles'] += fn.numPolygons
            if 'NORMAL' in attrs and not morph:
                normals = source.accessor(attrs['NORMAL'])
                ids = fn.getNormalIds()[1]; actual = fn.getNormals()
                for corner, vertex in enumerate(vertex_ids):
                    expected = om.MVector(*normals[vertex]).normal()
                    if not _close(tuple(actual[ids[corner]]), tuple(expected)):
                        raise ValueError('Normals edited: '+shape)
                checks['normals'] += len(normals)
            if 'TEXCOORD_0' in attrs:
                uvset = fn.currentUVSetName()
                u,v = fn.getUVs(uvset); uv_ids = fn.getAssignedUVs(uvset)[1]
                original = source.accessor(attrs['TEXCOORD_0'])
                if len(uv_ids) != len(vertex_ids) or any(not _close((u[uv_ids[c]],1-v[uv_ids[c]]), original[vertex]) for c,vertex in enumerate(vertex_ids)):
                    raise ValueError('UVs edited: '+shape)
                checks['uvs'] += len(original)
            if 'COLOR_0' in attrs:
                original = source.accessor(attrs['COLOR_0'])
                if not fn.getColorSetNames():
                    raise ValueError('Vertex colors missing: '+shape)
                colors = fn.getFaceVertexColors(fn.currentColorSetName())
                if any(not _close(tuple(colors[c]), original[vertex] if len(original[vertex])==4 else tuple(original[vertex])+(1,)) for c,vertex in enumerate(vertex_ids)):
                    raise ValueError('Vertex colors edited: '+shape)
                checks['colors'] += len(original)
            groups, assignments = fn.getConnectedShaders(0)
            if len(set(assignments)) != 1 or assignments[0] < 0:
                raise ValueError('Per-face material assignment changed: '+shape)
            group = om.MFnDependencyNode(groups[assignments[0]]).name()
            shader = (cmds.listConnections(group+'.surfaceShader',s=True,d=False) or [None])[0]
            material_index = primitive.get('material')
            if material_index is None:
                continue
            if not shader:
                raise ValueError('Source/Maya material missing: '+shape)
            if material_index in shader_map and shader_map[material_index] != shader:
                raise ValueError('Shared material split in Maya; source export needs an explicit remap')
            shader_map[material_index] = shader
            doc['materials'][material_index]['doubleSided'] = bool(cmds.getAttr(shape+'.doubleSided'))
        for child in node.get('children',[]):
            visit(child, transform)
    roots = doc['scenes'][doc.get('scene',0)]['nodes']
    if len(cmds.listRelatives(root, children=True, fullPath=True, type='transform') or []) != len(roots):
        raise ValueError('Import root hierarchy changed')
    for index in roots:
        visit(index, root)

    clips = json.loads(cmds.getAttr(root+'.glbAnimationClips')) if doc.get('animations') else []
    if len(clips) != len(doc.get('animations',[])):
        raise ValueError('Animation clip count changed')
    fps = om.MTime(1,om.MTime.kSeconds).asUnits(om.MTime.uiUnit())
    curves = {}
    for clip, animation in zip(clips,doc.get('animations',[])):
        if clip['name'] != animation.get('name',clip['name']):
            raise ValueError('Animation clip metadata no longer matches source')
        for channel in animation['channels']:
            target = channel['target']; index,path_ = target['node'],target['path']
            node = node_map[index]
            sampler = animation['samplers'][channel['sampler']]
            interpolation = sampler.get('interpolation','LINEAR')
            if interpolation != 'STEP' and not (path_ == 'weights' and interpolation == 'LINEAR'):
                raise ValueError('Lossless source animation verification currently supports STEP clips')
            times = [r[0] for r in source.accessor(sampler['input'])]
            values = source.accessor(sampler['output'])
            if path_ == 'weights':
                count = len(doc['meshes'][doc['nodes'][index]['mesh']]['primitives'][0]['targets'])
                values = [tuple(row[0] for row in values[i:i+count]) for i in range(0,len(values),count)]
                attributes = ['glbMorphWeight%d' % i for i in range(count)]
            else:
                prefix = {'translation':'translate','rotation':'rotate','scale':'scale'}[path_]
                attributes = [prefix+axis for axis in 'XYZ']
            key = (index,path_)
            if key not in curves:
                curves[key] = [_curve(node,attribute) for attribute in attributes]
            for n,(t,expected) in enumerate(zip(times,values)):
                # Evaluate just inside the STEP plateau to avoid Maya's saved
                # key-time tick rounding at the discontinuity itself.
                probe = t if interpolation == 'LINEAR' else (t + min(1e-5,(times[n+1]-t)/4) if n+1 < len(times) else t+1e-5)
                mt = om.MTime(clip['start']+probe*fps,om.MTime.uiUnit())
                actual = [fn.evaluate(mt) for fn in curves[key]]
                if path_ == 'rotation':
                    match = _quaternion_close(om.MEulerRotation(*actual).asQuaternion(),om.MQuaternion(*expected))
                else:
                    match = _close(actual,expected)
                if not match:
                    raise ValueError('Animation differs from source: %s / %s at %g seconds' % (clip['name'],node,t))
                if n+1 < len(times):
                    mid = om.MTime(clip['start']+(t+times[n+1])/2*fps,om.MTime.uiUnit())
                    midpoint = [fn.evaluate(mid) for fn in curves[key]]
                    expected_mid = [(a+b)/2 for a,b in zip(expected,values[n+1])] if interpolation == 'LINEAR' else actual
                    if not _close(midpoint,expected_mid,1e-6):
                        raise ValueError('Animation interpolation changed: '+node)
                checks['animation_samples'] += 1

    binary = bytearray(source.buffers[0][:doc['buffers'][0]['byteLength']])
    replacements = {}
    material_changes = []
    for index, shader in shader_map.items():
        material = doc['materials'][index]
        unlit = cmds.nodeType(shader) == 'surfaceShader'
        if cmds.nodeType(shader) not in ('standardSurface','surfaceShader'):
            raise ValueError('Unsupported scene shader: '+shader)
        attr = 'outColor' if unlit else 'baseColor'
        connection = cmds.listConnections(shader+'.'+attr,s=True,d=False,plugs=True) or []
        file_node = None
        if connection:
            multiply = connection[0].split('.')[0]
            if cmds.nodeType(multiply) != 'multiplyDivide' or cmds.getAttr(multiply+'.operation') != 1:
                raise ValueError('Unsupported base-color network: '+shader)
            color = list(cmds.getAttr(multiply+'.input2')[0])
            texture_input = cmds.listConnections(multiply+'.input1',s=True,d=False) or []
            if len(texture_input) != 1 or cmds.nodeType(texture_input[0]) != 'file':
                raise ValueError('Unsupported base-color texture network: '+shader)
            file_node = texture_input[0]
        else:
            color = list(cmds.getAttr(shader+'.'+attr)[0])
        pbr = material.setdefault('pbrMetallicRoughness',{})
        alpha = pbr.get('baseColorFactor',[1,1,1,1])[3]
        pbr['baseColorFactor'] = color+[alpha]
        if not unlit:
            pbr['metallicFactor'] = cmds.getAttr(shader+'.metalness')
            pbr['roughnessFactor'] = cmds.getAttr(shader+'.specularRoughness')
        if file_node:
            if 'baseColorTexture' not in pbr:
                raise ValueError('New texture assignment needs explicit mapping: '+shader)
            texture = doc['textures'][pbr['baseColorTexture']['index']]
            image_index = texture['source']
            filename = cmds.getAttr(file_node+'.fileTextureName')
            with open(filename,'rb') as stream:
                image_blob = stream.read()
            if image_index in replacements and replacements[image_index] != image_blob:
                raise ValueError('Shared GLB image edited differently on separate materials')
            replacements[image_index] = image_blob
            sampler = doc['samplers'][texture['sampler']] if 'sampler' in texture else {}
            sampler.update({'magFilter':9728 if cmds.getAttr(file_node+'.filterType')==0 else 9729,
                            'minFilter':9728 if cmds.getAttr(file_node+'.filterType')==0 else 9729})
            for axis,key in [('U','wrapS'),('V','wrapT')]:
                sampler[key] = 33071 if not cmds.getAttr(file_node+'.wrap'+axis) else 33648 if cmds.getAttr(file_node+'.mirror'+axis) else 10497
        if not unlit:
            inputs = cmds.listConnections(shader+'.opacityR',s=True,d=False,plugs=True) or []
            previous = material.get('alphaMode','OPAQUE')
            if not inputs and _close(cmds.getAttr(shader+'.opacity')[0],[1,1,1]):
                material['alphaMode'] = 'OPAQUE'
                material.pop('alphaCutoff',None)
                pbr['baseColorFactor'][3] = 1
            elif inputs and cmds.nodeType(inputs[0].split('.')[0]) == 'condition':
                condition = inputs[0].split('.')[0]
                material['alphaMode'] = 'MASK'
                material['alphaCutoff'] = cmds.getAttr(condition+'.secondTerm')
            else:
                raise ValueError('Unsupported edited opacity network: '+shader)
            if previous != material['alphaMode']:
                material_changes.append({'name':material.get('name'), 'before':previous, 'after':material['alphaMode']})
    for index,blob in replacements.items():
        image = doc['images'][index]
        original = source.view(image['bufferView']) if 'bufferView' in image else source.uri(image['uri'])
        if blob == original:
            continue
        mime = 'image/png' if blob.startswith(b'\x89PNG') else 'image/jpeg' if blob.startswith(b'\xff\xd8') else None
        if mime is None:
            raise ValueError('Export textures must be PNG/JPEG')
        binary.extend(b'\0'*(-len(binary)%4))
        view = {'buffer':0,'byteOffset':len(binary),'byteLength':len(blob)}
        binary.extend(blob); doc['bufferViews'].append(view)
        image['bufferView'] = len(doc['bufferViews'])-1
        image['mimeType'] = mime; image.pop('uri',None)
    doc['buffers'] = [{'byteLength':len(binary)}]
    doc['asset']['generator'] = 'Maya verified GLB material roundtrip'
    metadata = json.dumps(doc,separators=(',',':'),allow_nan=False).encode('utf8')
    metadata += b' '*(-len(metadata)%4); binary.extend(b'\0'*(-len(binary)%4))
    payload = struct.pack('<4sII',b'glTF',2,28+len(metadata)+len(binary))+struct.pack('<II',len(metadata),0x4E4F534A)+metadata+struct.pack('<II',len(binary),0x004E4942)+binary
    path = os.path.abspath(path)
    if os.path.exists(path):
        raise ValueError('Choose a new filename; export already exists: '+path)
    os.makedirs(os.path.dirname(path),exist_ok=True)
    fd,temporary = tempfile.mkstemp(dir=os.path.dirname(path),suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {'path':path,'source':source_path,'checks':checks,'clips':len(doc.get('animations',[])),
            'hierarchy_nodes':len(nodes),'skins':len(doc.get('skins',[])), 'materials':len(doc.get('materials',[])),
            'embedded_images':len(doc.get('images',[])), 'material_changes':material_changes,
            'sha256':hashlib.sha256(payload).hexdigest()}
