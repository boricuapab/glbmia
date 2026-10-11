"""Standalone Maya GLB translator; legacy plugin API with API 2.0 geometry."""
import base64
import bisect
import hashlib
import json
import math
import os
import re
import struct
import sys
import importlib.util
from urllib.parse import unquote

import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
import maya.OpenMayaMPx as ompx
import maya.cmds as cmds


def _name(value):
    value = re.sub(r'[^A-Za-z0-9_]', '_', value)
    return ('glb_' + value) if not value or value[0].isdigit() else value


class GLB:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        with open(path, 'rb') as stream:
            data = stream.read()
        if len(data) < 20:
            raise ValueError('Truncated GLB header')
        magic, version, length = struct.unpack_from('<4sII', data)
        if magic != b'glTF' or version != 2 or length != len(data):
            raise ValueError('Expected a valid glTF 2.0 GLB file')
        chunks = []
        offset = 12
        while offset < length:
            if offset + 8 > length:
                raise ValueError('Truncated GLB chunk header')
            size, kind = struct.unpack_from('<II', data, offset)
            offset += 8
            if offset + size > length or size % 4:
                raise ValueError('Invalid GLB chunk size')
            chunks.append((kind, data[offset:offset + size]))
            offset += size
        if not chunks or chunks[0][0] != 0x4E4F534A:
            raise ValueError('Missing GLB JSON chunk')
        self.doc = json.loads(chunks[0][1].decode('utf-8').rstrip('\x00 '))
        if self.doc.get('asset', {}).get('version') != '2.0':
            raise ValueError('Only glTF 2.0 is supported')
        unsupported = set(self.doc.get('extensionsRequired', [])) - {'KHR_materials_unlit'}
        if unsupported:
            raise ValueError('Required extensions are unsupported: ' + ', '.join(sorted(unsupported)))
        binary = next((blob for kind, blob in chunks if kind == 0x004E4942), b'')
        self.buffers = []
        self._accessors = {}
        for index, buffer in enumerate(self.doc.get('buffers', [])):
            blob = self.uri(buffer['uri']) if 'uri' in buffer else binary
            if len(blob) < buffer['byteLength']:
                raise ValueError('Truncated buffer %d' % index)
            self.buffers.append(blob)

    def uri(self, uri):
        if uri.startswith('data:'):
            header, payload = uri.split(',', 1)
            if ';base64' not in header:
                raise ValueError('Only base64 data URIs are supported')
            return base64.b64decode(payload)
        if '://' in uri or os.path.isabs(unquote(uri)):
            raise ValueError('Only relative local resource URIs are supported')
        with open(os.path.join(os.path.dirname(self.path), unquote(uri)), 'rb') as stream:
            return stream.read()

    def view(self, index):
        view = self.doc['bufferViews'][index]
        start = view.get('byteOffset', 0)
        blob = self.buffers[view['buffer']]
        if start < 0 or start + view['byteLength'] > len(blob):
            raise ValueError('Buffer view out of range')
        return blob[start:start + view['byteLength']]

    def unpack(self, view_index, offset, count, component, width, strided=True):
        codes = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
        fmt = '<' + codes[component] * width
        size = struct.calcsize(fmt)
        view = self.doc['bufferViews'][view_index]
        stride = view.get('byteStride', size) if strided else size
        blob = self.view(view_index)
        if offset < 0 or stride < size or (count and offset + (count - 1) * stride + size > len(blob)):
            raise ValueError('Accessor out of buffer bounds')
        return [struct.unpack_from(fmt, blob, offset + i * stride) for i in range(count)]

    def accessor(self, index):
        if index in self._accessors:
            return self._accessors[index]
        a = self.doc['accessors'][index]
        widths = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}
        if a['type'] not in widths:
            raise ValueError('Unsupported accessor type: ' + a['type'])
        width = widths[a['type']]
        component = a['componentType']
        values = self.unpack(a['bufferView'], a.get('byteOffset', 0), a['count'], component, width) if 'bufferView' in a else [(0,) * width for _ in range(a['count'])]
        if 'sparse' in a:
            s = a['sparse']
            ix, va = s['indices'], s['values']
            indices = self.unpack(ix['bufferView'], ix.get('byteOffset', 0), s['count'], ix['componentType'], 1, False)
            replacements = self.unpack(va['bufferView'], va.get('byteOffset', 0), s['count'], component, width, False)
            for (target,), value in zip(indices, replacements):
                if target >= len(values):
                    raise ValueError('Sparse accessor index out of range')
                values[target] = value
        if a.get('normalized') and component != 5126:
            limits = {5120: 127., 5121: 255., 5122: 32767., 5123: 65535., 5125: 4294967295.}
            values = [tuple(max(-1., x / limits[component]) for x in row) for row in values]
        self._accessors[index] = values
        return values

    def image_path(self, index, directory):
        image = self.doc['images'][index]
        blob = self.view(image['bufferView']) if 'bufferView' in image else self.uri(image['uri'])
        if blob.startswith(b'\x89PNG\r\n\x1a\n'):
            extension = '.png'
        elif blob.startswith(b'\xff\xd8'):
            extension = '.jpg'
        else:
            raise ValueError('Only PNG and JPEG textures are supported')
        os.makedirs(directory, exist_ok=True)
        filename = '%s_%s%s' % (_name(image.get('name', 'image_%d' % index)), hashlib.sha256(blob).hexdigest()[:16], extension)
        path = os.path.abspath(os.path.join(directory, filename))
        if not os.path.exists(path):
            with open(path + '.tmp', 'wb') as stream:
                stream.write(blob)
            os.replace(path + '.tmp', path)
        return path.replace('\\', '/')


def _texture(glb, info, directory, created, color=True):
    if info.get('texCoord', 0) != 0 or info.get('extensions'):
        raise ValueError('Only UV0 textures without texture extensions are supported')
    texture = glb.doc['textures'][info['index']]
    file_node = cmds.shadingNode('file', asTexture=True, name='glbTexture')
    created.append(file_node)
    cmds.setAttr(file_node + '.ignoreColorSpaceFileRules', True)
    cmds.setAttr(file_node + '.fileTextureName', glb.image_path(texture['source'], directory), type='string')
    cmds.setAttr(file_node + '.colorSpace', 'sRGB' if color else 'Raw', type='string')
    sampler = glb.doc.get('samplers', [])[texture['sampler']] if 'sampler' in texture else {}
    # File nodes can use the mesh's UVs directly; no place2dTexture is needed.
    for axis, key in [('U', 'wrapS'), ('V', 'wrapT')]:
        mode = sampler.get(key, 10497)
        cmds.setAttr(file_node + '.wrap' + axis, mode != 33071)
        cmds.setAttr(file_node + '.mirror' + axis, mode == 33648)
    nearest = sampler.get('magFilter') == 9728 and sampler.get('minFilter', 9728) == 9728
    cmds.setAttr(file_node + '.filterType', 0 if nearest else 1)
    return file_node


def _multiply(source, factor, created, name='glbFactor'):
    node = cmds.shadingNode('multiplyDivide', asUtility=True, name=name)
    created.append(node)
    cmds.connectAttr(source, node + '.input1')
    cmds.setAttr(node + '.input2', *factor, type='double3')
    return node + '.output'


def _material(glb, material, index, directory, created):
    unlit = 'KHR_materials_unlit' in material.get('extensions', {})
    shader = cmds.shadingNode('surfaceShader' if unlit else 'standardSurface', asShader=True,
                             name=_name(material.get('name', 'glbMaterial_%d' % index)))
    created.append(shader)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + 'SG')
    created.append(sg)
    cmds.connectAttr(shader + '.outColor', sg + '.surfaceShader')
    pbr = material.get('pbrMetallicRoughness', {})
    factor = pbr.get('baseColorFactor', [1, 1, 1, 1])
    color_attr = shader + ('.outColor' if unlit else '.baseColor')
    cmds.setAttr(color_attr, *factor[:3], type='double3')
    if not unlit:
        cmds.setAttr(shader + '.base', 1)
        cmds.setAttr(shader + '.metalness', pbr.get('metallicFactor', 1))
        cmds.setAttr(shader + '.specularRoughness', pbr.get('roughnessFactor', 1))
    base = None
    if 'baseColorTexture' in pbr:
        base = _texture(glb, pbr['baseColorTexture'], directory, created)
        cmds.connectAttr(_multiply(base + '.outColor', factor[:3], created), color_attr)
    mode = material.get('alphaMode', 'OPAQUE')
    if mode not in ('OPAQUE', 'MASK', 'BLEND'):
        raise ValueError('Unsupported alpha mode: ' + mode)
    if mode != 'OPAQUE':
        alpha = cmds.shadingNode('multiplyDivide', asUtility=True, name='glbAlphaFactor')
        created.append(alpha)
        cmds.setAttr(alpha + '.input2X', factor[3])
        if base:
            cmds.connectAttr(base + '.outAlpha', alpha + '.input1X')
        else:
            cmds.setAttr(alpha + '.input1X', 1)
        plug = alpha + '.outputX'
        if mode == 'MASK':
            cutoff = cmds.shadingNode('condition', asUtility=True, name='glbAlphaCutoff')
            created.append(cutoff)
            cmds.setAttr(cutoff + '.operation', 3)
            cmds.setAttr(cutoff + '.secondTerm', material.get('alphaCutoff', 0.5))
            cmds.setAttr(cutoff + '.colorIfTrue', 1, 1, 1, type='double3')
            cmds.setAttr(cutoff + '.colorIfFalse', 0, 0, 0, type='double3')
            cmds.connectAttr(plug, cutoff + '.firstTerm')
            plug = cutoff + '.outColorR'
        if unlit:
            reverse = cmds.shadingNode('reverse', asUtility=True, name='glbTransparency')
            created.append(reverse)
            for axis in 'XYZ':
                cmds.connectAttr(plug, reverse + '.input' + axis)
            cmds.connectAttr(reverse + '.output', shader + '.outTransparency')
        else:
            for axis in 'RGB':
                cmds.connectAttr(plug, shader + '.opacity' + axis)
    if not unlit and 'metallicRoughnessTexture' in pbr:
        packed = _texture(glb, pbr['metallicRoughnessTexture'], directory, created, color=False)
        output = _multiply(packed + '.outColor', [1, pbr.get('roughnessFactor', 1), pbr.get('metallicFactor', 1)], created)
        cmds.connectAttr(output + 'Y', shader + '.specularRoughness')
        cmds.connectAttr(output + 'Z', shader + '.metalness')
    if not unlit and 'normalTexture' in material:
        normal = _texture(glb, material['normalTexture'], directory, created, color=False)
        bump = cmds.shadingNode('bump2d', asUtility=True, name='glbNormal')
        created.append(bump)
        cmds.setAttr(bump + '.bumpInterp', 1)
        cmds.setAttr(bump + '.bumpDepth', material['normalTexture'].get('scale', 1))
        cmds.connectAttr(normal + '.outAlpha', bump + '.bumpValue')
        cmds.connectAttr(bump + '.outNormal', shader + '.normalCamera')
    if not unlit and ('emissiveFactor' in material or 'emissiveTexture' in material):
        emission = material.get('emissiveFactor', [0, 0, 0])
        cmds.setAttr(shader + '.emission', 1)
        cmds.setAttr(shader + '.emissionColor', *emission, type='double3')
        if 'emissiveTexture' in material:
            texture = _texture(glb, material['emissiveTexture'], directory, created)
            cmds.connectAttr(_multiply(texture + '.outColor', emission, created), shader + '.emissionColor')
    if 'occlusionTexture' in material:
        om.MGlobal.displayWarning('GLB occlusion texture is not connected to the Maya material.')
    return sg


def _animation_data(glb):
    clips = []
    for index, animation in enumerate(glb.doc.get('animations', [])):
        channels = []
        seen = set()
        for channel in animation['channels']:
            target = channel['target']
            if target.get('path') not in ('translation', 'rotation', 'scale', 'weights'):
                raise ValueError('Unsupported animation target path')
            key = (target['node'], target['path'])
            if key in seen:
                raise ValueError('Duplicate animation target')
            seen.add(key)
            sampler = animation['samplers'][channel['sampler']]
            interpolation = sampler.get('interpolation', 'LINEAR')
            if interpolation not in ('STEP', 'LINEAR'):
                raise ValueError('Animation interpolation unsupported: ' + interpolation)
            times = [row[0] for row in glb.accessor(sampler['input'])]
            values = glb.accessor(sampler['output'])
            width = 4 if target['path'] == 'rotation' else 3
            if target['path'] == 'weights':
                node = glb.doc['nodes'][target['node']]
                mesh = glb.doc['meshes'][node['mesh']]
                width = len(mesh['primitives'][0].get('targets', []))
                if not width or len(values) != len(times) * width or any(len(v) != 1 for v in values):
                    raise ValueError('Invalid morph weight animation dimensions')
                values = [tuple(v[0] for v in values[i:i+width]) for i in range(0, len(values), width)]
            if not times or len(times) != len(values) or any(len(v) != width for v in values):
                raise ValueError('Invalid animation sampler dimensions')
            if times[0] < 0 or any(a >= b for a, b in zip(times, times[1:])):
                raise ValueError('Animation times must be nonnegative and strictly increasing')
            if any(not math.isfinite(v) for row in values for v in row) or any(not math.isfinite(t) for t in times):
                raise ValueError('Nonfinite animation data')
            if target['path'] == 'rotation' and any(sum(x*x for x in v) < 1e-12 for v in values):
                raise ValueError('Invalid zero animation quaternion')
            channels.append((key, interpolation, times, values))
        if channels:
            clips.append({'name': animation.get('name', 'animation_%02d' % index), 'channels': channels,
                          'duration': max(c[2][-1] for c in channels)})
    return clips


def _rotation_samples(times, values, interpolation, fps):
    if interpolation == 'STEP':
        samples = list(zip(times, values))
    else:
        # Maya uses Euler curves. Bake glTF quaternion slerp at scene-frame times
        # and at every original key; source keys remain exact.
        sample_times = sorted(set(times + [i / fps for i in range(int(math.ceil(times[0] * fps)), int(math.floor(times[-1] * fps)) + 1)]))
        samples = []
        for t in sample_times:
            left = max(0, min(len(times) - 1, bisect.bisect_right(times, t) - 1))
            q = om.MQuaternion(*values[left]).normal()
            if left + 1 < len(times):
                blend = (t - times[left]) / (times[left + 1] - times[left])
                q = om.MQuaternion.slerp(q, om.MQuaternion(*values[left + 1]).normal(), blend)
            samples.append((t, (q.x, q.y, q.z, q.w)))
    result = []
    previous = None
    for t, value in samples:
        euler = om.MQuaternion(*value).normal().asEulerRotation()
        angles = (euler.x, euler.y, euler.z)
        if previous is not None:
            # Unwrap by whole revolutions only. Maya 2023's closestSolution
            # can change the actual orientation at some singular rotations.
            angles = tuple(a + 2 * math.pi * round((p - a) / (2 * math.pi)) for a, p in zip(angles, previous))
        previous = angles
        result.append((t, angles))
    return result


def _import_animations(glb, root, node_map, clips, created):
    if not clips:
        return
    fps = om.MTime(1, om.MTime.kSeconds).asUnits(om.MTime.uiUnit())
    start = 1.0
    metadata = []
    curve_data = {}
    targets = {channel[0] for clip in clips for channel in clip['channels']}
    for clip in clips:
        channels = {channel[0]: channel for channel in clip['channels']}
        end = start + clip['duration'] * fps
        metadata.append({'name': clip['name'], 'start': start, 'end': end})
        for (index, path) in sorted(targets):
            if index not in node_map:
                raise ValueError('Animated node is outside the imported default scene')
            if path != 'weights' and 'matrix' in glb.doc['nodes'][index]:
                raise ValueError('Animated nodes must use TRS, not matrix transforms')
            if (index, path) in channels:
                _, interpolation, times, values = channels[index, path]
                samples = _rotation_samples(times, values, interpolation, fps) if path == 'rotation' else list(zip(times, values))
            else:
                interpolation = 'STEP'
                defaults = {'translation': [0, 0, 0], 'rotation': [0, 0, 0, 1], 'scale': [1, 1, 1]}
                if path == 'weights':
                    node = glb.doc['nodes'][index]
                    mesh = glb.doc['meshes'][node['mesh']]
                    count = len(mesh['primitives'][0].get('targets', []))
                    values = node.get('weights', mesh.get('weights', [0.0] * count))
                else:
                    values = glb.doc['nodes'][index].get(path, defaults[path])
                samples = _rotation_samples([0], [values], 'STEP', fps) if path == 'rotation' else [(0, values)]
            if samples[0][0] > 0:
                samples.insert(0, (0, samples[0][1]))
            if samples[-1][0] < clip['duration']:
                samples.append((clip['duration'], samples[-1][1]))
            plugs = [node_map[index]+'.glbMorphWeight%d' % i for i in range(len(samples[0][1]))] if path == 'weights' else [node_map[index]+'.'+{'translation': 'translate', 'rotation': 'rotate', 'scale': 'scale'}[path]+suffix for suffix in 'XYZ']
            for axis, key in enumerate(plugs):
                data = curve_data.setdefault(key, {'path': path, 'times': [], 'values': [], 'tangents': []})
                for t, value in samples:
                    # Local geometry/transforms use Maya's internal centimeters;
                    # the import root scales glTF meters to centimeters once.
                    v = value[axis]
                    data['times'].append(om.MTime(start + t * fps, om.MTime.uiUnit()))
                    data['values'].append(v)
                    data['tangents'].append(oma.MFnAnimCurve.kTangentStep if interpolation == 'STEP' else oma.MFnAnimCurve.kTangentLinear)
                # Hold the final pose through the one-frame gap between clips.
                data['tangents'][-1] = oma.MFnAnimCurve.kTangentStep
        start = math.ceil(end) + 1
    for plug_name, data in curve_data.items():
        selection = om.MSelectionList()
        selection.add(plug_name)
        fn = oma.MFnAnimCurve()
        curve_type = {'translation': fn.kAnimCurveTL, 'rotation': fn.kAnimCurveTA, 'scale': fn.kAnimCurveTU, 'weights': fn.kAnimCurveTU}[data['path']]
        obj = fn.create(selection.getPlug(0), curve_type)
        created.append(om.MFnDependencyNode(obj).name())
        # A file may mix step and linear clips on a channel.
        default = data['tangents'][0]
        fn.addKeys(om.MTimeArray(data['times']), om.MDoubleArray(data['values']), fn.kTangentLinear, default)
        for i, tangent in enumerate(data['tangents']):
            if tangent != default:
                fn.setOutTangentType(i, tangent)
    cmds.addAttr(root, longName='glbAnimationClips', dataType='string')
    cmds.setAttr(root + '.glbAnimationClips', json.dumps(metadata), type='string')
    cmds.playbackOptions(minTime=metadata[0]['start'], maxTime=metadata[0]['end'],
                         animationStartTime=metadata[0]['start'], animationEndTime=metadata[-1]['end'])
    cmds.currentTime(metadata[0]['start'])


def animation_clips(root):
    """Return imported clip names and frame ranges stored in the Maya scene."""
    return json.loads(cmds.getAttr(root + '.glbAnimationClips')) if cmds.attributeQuery('glbAnimationClips', node=root, exists=True) else []


def select_animation_clip(root, index=0):
    clip = animation_clips(root)[index]
    cmds.playbackOptions(minTime=clip['start'], maxTime=clip['end'],
                         animationStartTime=clip['start'], animationEndTime=animation_clips(root)[-1]['end'])
    cmds.currentTime(clip['start'])
    cmds.select(root)


def show_animation_clips(root=None):
    """Choose a character and clip without re-importing or deleting animation."""
    roots = [root] if root else [n for n in cmds.ls(type='transform', long=True) if cmds.attributeQuery('glbAnimationClips', node=n, exists=True)]
    if not roots:
        cmds.warning('No imported GLB animation clips in this scene.')
        return
    if cmds.window('glbAnimationPicker', exists=True):
        cmds.deleteUI('glbAnimationPicker')
    window = cmds.window('glbAnimationPicker', title='GLB Animation Clips', widthHeight=(420, 320))
    cmds.columnLayout(adjustableColumn=True)
    character = cmds.optionMenu(label='Character')
    for node in roots:
        cmds.menuItem(label=node)
    listing = cmds.textScrollList(height=230, allowMultiSelection=False)
    def populate(*args):
        node = cmds.optionMenu(character, q=True, value=True)
        cmds.textScrollList(listing, edit=True, removeAll=True)
        for clip in animation_clips(node):
            cmds.textScrollList(listing, edit=True, append='%s  [%g - %g]' % (clip['name'], clip['start'], clip['end']))
        cmds.textScrollList(listing, edit=True, selectIndexedItem=1)
    def choose(*args):
        node = cmds.optionMenu(character, q=True, value=True)
        selected = cmds.textScrollList(listing, q=True, selectIndexedItem=True)
        if not selected:
            return
        select_animation_clip(node, selected[0] - 1)
    cmds.optionMenu(character, edit=True, changeCommand=populate)
    cmds.textScrollList(listing, edit=True, doubleClickCommand=choose)
    cmds.button(label='Set Playback Range', command=choose)
    populate()
    cmds.showWindow(window)


def _triangles(indices, mode):
    if mode == 4:
        if len(indices) % 3:
            raise ValueError('Triangle index count must be divisible by three')
        return indices
    if mode == 5:
        triangles = [(indices[i + (i % 2)], indices[i + 1 - (i % 2)], indices[i + 2]) for i in range(len(indices) - 2)]
    elif mode == 6:
        triangles = [(indices[0], indices[i], indices[i + 1]) for i in range(1, len(indices) - 1)]
    else:
        raise ValueError('Only triangles, triangle strips and triangle fans are supported')
    return [x for triangle in triangles if len(set(triangle)) == 3 for x in triangle]


def import_glb(path, texture_directory=None):
    """Add the default GLB scene. Return its root transform. glTF metres become Maya units."""
    glb = GLB(path)
    doc = glb.doc
    # Preflight geometry before changing the scene.
    geometry = {}
    morphs = {}
    for mi, mesh in enumerate(doc.get('meshes', [])):
        target_counts = {len(p.get('targets', [])) for p in mesh['primitives']}
        if len(target_counts) != 1:
            raise ValueError('All mesh primitives must have the same morph target count')
        for pi, primitive in enumerate(mesh['primitives']):
            if primitive.get('extensions'):
                raise ValueError('Primitive extensions are unsupported')
            attrs = primitive['attributes']
            points = glb.accessor(attrs['POSITION'])
            indices = [row[0] for row in glb.accessor(primitive['indices'])] if 'indices' in primitive else list(range(len(points)))
            indices = _triangles(indices, primitive.get('mode', 4))
            if not indices or any(i < 0 or i >= len(points) for i in indices):
                raise ValueError('Invalid or empty mesh indices')
            extra = {key: glb.accessor(value) for key, value in attrs.items() if key in ('NORMAL', 'TEXCOORD_0', 'COLOR_0')}
            if any(len(values) != len(points) for values in extra.values()):
                raise ValueError('Vertex attribute counts differ')
            geometry[mi, pi] = (points, indices, extra)
            deltas = []
            for target in primitive.get('targets', []):
                if set(target) - {'POSITION', 'NORMAL'}:
                    raise ValueError('Only POSITION and NORMAL morph attributes are supported')
                decoded = {key: glb.accessor(value) for key, value in target.items()}
                if any(len(rows) != len(points) or any(len(row) != 3 or any(not math.isfinite(v) for v in row) for row in rows) for rows in decoded.values()):
                    raise ValueError('Invalid morph target vertex data')
                deltas.append(decoded.get('POSITION', [(0.0, 0.0, 0.0)] * len(points)))
            morphs[mi, pi] = deltas
    for node in doc.get('nodes', []):
        if 'mesh' in node:
            mesh = doc['meshes'][node['mesh']]
            count = len(mesh['primitives'][0].get('targets', []))
            weights = node.get('weights', mesh.get('weights', [0.0] * count))
            if len(weights) != count or any(not math.isfinite(v) for v in weights):
                raise ValueError('Invalid default morph weights')
    if any('skin' in node for node in doc.get('nodes', [])):
        raise ValueError('Skinned meshes are unsupported; transform animation is supported')
    clips = _animation_data(glb)
    texture_directory = texture_directory or os.path.splitext(glb.path)[0] + '_textures'
    root = cmds.createNode('transform', name=_name(os.path.splitext(os.path.basename(path))[0]) + '_GLB')
    created_materials = []
    try:
        scale = 100.0  # MFnMesh and matrix translations use internal centimeters.
        cmds.setAttr(root + '.scale', scale, scale, scale)
        if cmds.upAxis(q=True, axis=True) == 'z':
            cmds.setAttr(root + '.rotateX', 90)
        cmds.addAttr(root, longName='glbSource', dataType='string')
        cmds.setAttr(root + '.glbSource', glb.path, type='string')
        materials = []
        for i, material in enumerate(doc.get('materials', [])):
            materials.append(_material(glb, material, i, texture_directory, created_materials))
        nodes = doc.get('nodes', [])
        node_map = {}
        def visit(index, parent, ancestors):
            if index in ancestors:
                raise ValueError('Cycle in GLB node hierarchy')
            node = nodes[index]
            transform = cmds.createNode('transform', name=_name(node.get('name', 'glbNode_%d' % index)), parent=parent)
            transform = cmds.ls(transform, long=True)[0]
            if index in node_map:
                raise ValueError('A GLB node cannot have multiple parents')
            node_map[index] = transform
            if 'matrix' in node:
                cmds.xform(transform, objectSpace=True, matrix=node['matrix'])
            else:
                t = node.get('translation', [0, 0, 0])
                s = node.get('scale', [1, 1, 1])
                q = om.MQuaternion(*node.get('rotation', [0, 0, 0, 1]))
                matrix = om.MTransformationMatrix()
                matrix.setTranslation(om.MVector(*t), om.MSpace.kTransform)
                matrix.setRotation(q)
                matrix.setScale(s, om.MSpace.kTransform)
                cmds.xform(transform, objectSpace=True, matrix=list(matrix.asMatrix()))
            if 'mesh' in node:
                mi = node['mesh']
                mesh = doc['meshes'][mi]
                weights = node.get('weights', mesh.get('weights', [0.0] * len(mesh['primitives'][0].get('targets', []))))
                for wi, weight in enumerate(weights):
                    cmds.addAttr(transform, longName='glbMorphWeight%d' % wi, attributeType='double', keyable=True, defaultValue=weight)
                for pi, primitive in enumerate(doc['meshes'][mi]['primitives']):
                    points, indices, extra = geometry[mi, pi]
                    selection = om.MSelectionList()
                    selection.add(transform)
                    fn = om.MFnMesh()
                    obj = fn.create(om.MPointArray([om.MPoint(*p) for p in points]), [3] * (len(indices) // 3), indices, parent=selection.getDependNode(0))
                    shape = om.MFnDagNode(obj).fullPathName()
                    cmds.rename(shape, _name(node.get('name', 'glbMesh')) + 'Shape')
                    shape = om.MFnDagNode(obj).fullPathName()
                    if 'NORMAL' in extra:
                        fn.setVertexNormals(om.MVectorArray([om.MVector(*n) for n in extra['NORMAL']]), list(range(len(points))))
                    if 'TEXCOORD_0' in extra:
                        uv = extra['TEXCOORD_0']
                        fn.setUVs([row[0] for row in uv], [1 - row[1] for row in uv])
                        fn.assignUVs([3] * (len(indices) // 3), indices)
                    if 'COLOR_0' in extra:
                        colors = [om.MColor(tuple(row) if len(row) == 4 else tuple(row) + (1.,)) for row in extra['COLOR_0']]
                        fn.setVertexColors(colors, list(range(len(points))))
                        # White vertex colors must not replace the textured material
                        # in Maya's viewport. Preserve nonwhite colors for display.
                        cmds.setAttr(shape + '.displayColors', any(any(abs(c - 1) > 1e-6 for c in row[:3]) for row in extra['COLOR_0']))
                    cmds.setAttr(shape + '.doubleSided', doc.get('materials', [{}])[primitive['material']].get('doubleSided', False) if 'material' in primitive else False)
                    cmds.sets(shape, edit=True, forceElement=materials[primitive['material']] if 'material' in primitive else 'initialShadingGroup')
                    if morphs[mi, pi]:
                        # Native blend shapes retain target deltas after temporary target meshes are removed.
                        cmds.polyNormalPerVertex(shape, unFreezeNormal=True)
                        blend = cmds.blendShape(shape, origin='local', name=_name(node.get('name', 'glbMesh'))+'_Morphs')[0]
                        created_materials.append(blend)
                        cmds.setAttr(blend+'.supportNegativeWeights', True)
                        for wi, delta in enumerate(morphs[mi, pi]):
                            temporary = cmds.createNode('transform', name='glbMorphTemporary')
                            try:
                                sel = om.MSelectionList(); sel.add(temporary)
                                target_fn = om.MFnMesh()
                                target_fn.create(om.MPointArray([om.MPoint(*(p[a]+d[a] for a in range(3))) for p,d in zip(points, delta)]), [3]*(len(indices)//3), indices, parent=sel.getDependNode(0))
                                cmds.blendShape(blend, edit=True, target=(shape, wi, temporary, 1.0))
                            finally:
                                cmds.delete(temporary)
                            cmds.connectAttr(transform+'.glbMorphWeight%d' % wi, blend+'.weight[%d]' % wi)
                        if any('NORMAL' in target for target in primitive['targets']):
                            om.MGlobal.displayWarning('GLB morph normals are recalculated from deformed geometry; supplied NORMAL deltas are not imported.')
            for child in node.get('children', []):
                visit(child, transform, ancestors | {index})
        scenes = doc.get('scenes', [])
        roots = scenes[doc.get('scene', 0)].get('nodes', []) if scenes else [i for i in range(len(nodes)) if i not in {c for n in nodes for c in n.get('children', [])}]
        for index in roots:
            visit(index, root, set())
        _import_animations(glb, root, node_map, clips, created_materials)
        cmds.select(root, replace=True)
        om.MGlobal.displayInfo('Imported GLB: ' + glb.path)
        return root
    except Exception:
        cmds.delete(root)
        if created_materials:
            cmds.delete(created_materials)
        raise


def export_glb(path, selected=True, pyrite64=True):
    """Export static native Maya meshes; Pyrite64 mode emits Fast64 material extras."""
    _support_path()
    from glbformaya_support import static_export
    return static_export.export_glb(path, selected, pyrite64)


def _support_path():
    filename = globals().get('__file__', _support_path.__code__.co_filename)
    scripts = os.path.abspath(os.path.join(os.path.dirname(filename), '..', 'scripts'))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def export_imported_glb(path, root=None):
    """Preserve a verified imported GLB's animations and source mesh data."""
    _support_path()
    from glbformaya_support import roundtrip
    return roundtrip.export_imported_glb(path, root, glb_reader=GLB, name_function=_name)



def show_roundtrip_dialog(*_):
    roots = [n for n in cmds.ls(type='transform', long=True)
             if cmds.attributeQuery('glbSource', node=n, exists=True)]
    selected = cmds.ls(selection=True, long=True) or []
    chosen = [r for r in roots if any(s == r or s.startswith(r+'|') for s in selected)]
    if len(chosen) == 1:
        root = chosen[0]
    elif len(roots) == 1:
        root = roots[0]
    else:
        cmds.error('Select one imported GLB group to export.')
        return
    paths = cmds.fileDialog2(fileMode=0, fileFilter='glTF Binary (*.glb)',
                            caption='Export imported GLB with animations')
    if paths:
        result = export_imported_glb(paths[0], root)
        om.MGlobal.displayInfo('Exported GLB: '+result['path'])


def show_export_dialog(*_):
    paths = cmds.fileDialog2(fileMode=0, fileFilter='glTF Binary (*.glb)', caption='Export selected meshes for Pyrite64')
    if paths:
        result = export_glb(paths[0], selected=True, pyrite64=True)
        om.MGlobal.displayInfo('Exported GLB: '+result['path'])


class GLBTranslator(ompx.MPxFileTranslator):
    @staticmethod
    def creator():
        return ompx.asMPxPtr(GLBTranslator())

    def haveReadMethod(self):
        return True

    def haveWriteMethod(self):
        return True

    def canBeOpened(self):
        return False

    def defaultExtension(self):
        return 'glb'

    def filter(self):
        return '*.glb'

    def identifyFile(self, file_object, buffer, size):
        return self.kIsMyFileType if file_object.resolvedFullName().lower().endswith('.glb') else self.kNotMyFileType

    def reader(self, file_object, options, access_mode):
        root = import_glb(file_object.resolvedFullName())
        if animation_clips(root) and not cmds.about(batch=True):
            # Maya restores playback settings after its file translator returns.
            # Apply the first clip's range after that final import step.
            import maya.utils
            maya.utils.executeDeferred(lambda: select_animation_clip(root) if cmds.objExists(root) else None)

    def writer(self, file_object, options, access_mode):
        selected = access_mode == self.kExportActiveAccessMode
        roots = [n for n in cmds.ls(type='transform', long=True) if cmds.attributeQuery('glbSource', node=n, exists=True)]
        selection = cmds.ls(selection=True, long=True) or []
        chosen = [r for r in roots if not selected or any(s == r or s.startswith(r+'|') for s in selection)]
        if len(chosen) == 1 and (cmds.attributeQuery('glbAnimationClips', node=chosen[0], exists=True) or cmds.ls(chosen[0],dag=True,type='mesh')):
            if selected and any(s != chosen[0] for s in selection):
                raise ValueError('Select the entire imported GLB group to preserve its animation')
            export_imported_glb(file_object.resolvedFullName(), chosen[0])
        elif chosen:
            raise ValueError('Select one imported GLB group for export')
        else:
            export_glb(file_object.resolvedFullName(), selected=selected, pyrite64=True)


def initializePlugin(plugin):
    ompx.MFnPlugin(plugin, 'glbformaya', '1.4.2', 'Any').registerFileTranslator('glTF Binary', None, GLBTranslator.creator)
    if not cmds.about(batch=True):
        menu = cmds.menu('glbImporterMenu', label='glbformaya', parent='MayaWindow', tearOff=True)
        cmds.menuItem(label='Animation Clips...', parent=menu,
                      command=lambda *args: show_animation_clips())
        cmds.menuItem(label='Export Imported GLB with Animations...', parent=menu, command=show_roundtrip_dialog)
        cmds.menuItem(label='Export Selected for Pyrite64...', parent=menu, command=show_export_dialog)


def uninitializePlugin(plugin):
    if not cmds.about(batch=True) and cmds.window('glbAnimationPicker', exists=True):
        cmds.deleteUI('glbAnimationPicker')
    if not cmds.about(batch=True) and cmds.menu('glbImporterMenu', exists=True):
        cmds.deleteUI('glbImporterMenu')
    ompx.MFnPlugin(plugin).deregisterFileTranslator('glTF Binary')
