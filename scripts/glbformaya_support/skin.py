"""Native linear skinning with glTF inverse bind matrices and joint ordering."""
import math
import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma


def preflight(glb):
    doc = glb.doc
    for skin in doc.get('skins', []):
        joints = skin.get('joints', [])
        if not joints or len(set(joints)) != len(joints) or any(i < 0 or i >= len(doc['nodes']) for i in joints):
            raise ValueError('Invalid skin joint list')
        if 'inverseBindMatrices' in skin:
            matrices = glb.accessor(skin['inverseBindMatrices'])
            if len(matrices) != len(joints) or any(len(m) != 16 or any(not math.isfinite(v) for v in m) for m in matrices):
                raise ValueError('Invalid inverse bind matrices')
    for node in doc.get('nodes', []):
        if 'skin' not in node: continue
        if 'mesh' not in node or node['skin'] < 0 or node['skin'] >= len(doc.get('skins', [])):
            raise ValueError('Invalid skinned mesh node')
        count = len(doc['skins'][node['skin']]['joints'])
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            attrs = primitive['attributes']
            if any(k.startswith(('JOINTS_', 'WEIGHTS_')) and k not in ('JOINTS_0','WEIGHTS_0') for k in attrs):
                raise ValueError('Skin import currently supports four influences per vertex')
            if 'JOINTS_0' not in attrs or 'WEIGHTS_0' not in attrs:
                raise ValueError('Skinned primitive requires JOINTS_0 and WEIGHTS_0')
            joints = glb.accessor(attrs['JOINTS_0']); weights = glb.accessor(attrs['WEIGHTS_0'])
            vertices = len(glb.accessor(attrs['POSITION']))
            if len(joints) != vertices or len(weights) != vertices:
                raise ValueError('Skin vertex counts differ')
            for js, ws in zip(joints, weights):
                if len(js) != 4 or len(ws) != 4 or any(i != int(i) or i < 0 or i >= count for i in js) or any(not math.isfinite(w) or w < 0 for w in ws) or abs(sum(ws)-1) > 0.02:
                    raise ValueError('Invalid joint indices or skin weights')


def bind_skins(glb, root, nodes, shapes, created):
    root_matrix = om.MMatrix(cmds.xform(root,q=True,worldSpace=True,matrix=True))
    doc = glb.doc
    for index, node in enumerate(doc.get('nodes', [])):
        if 'skin' not in node or index not in nodes: continue
        skin = doc['skins'][node['skin']]
        if any(j not in nodes for j in skin['joints']):
            raise ValueError('Skin joint is outside the imported scene')
        influences = [nodes[j] for j in skin['joints']]
        matrices = glb.accessor(skin['inverseBindMatrices']) if 'inverseBindMatrices' in skin else [list(om.MMatrix())]*len(influences)
        # glTF ignores the mesh node's transforms for skinning. Keep its node
        # hierarchy/curves, but put the evaluated geometry directly under the
        # import root so its own animated TRS cannot be applied a second time.
        holder = cmds.createNode('transform',name=nodes[index].split('|')[-1]+'_SkinnedGeometry',parent=root)
        holder = cmds.ls(holder,long=True)[0]
        for pi, primitive in enumerate(doc['meshes'][node['mesh']]['primitives']):
            shape = shapes[index, pi]
            shape = cmds.parent(shape,holder,shape=True,relative=True)[0]
            shape = cmds.ls(shape,long=True)[0]
            shapes[index,pi] = shape
            cluster = cmds.skinCluster(influences, shape, toSelectedBones=True, normalizeWeights=0,
                                       obeyMaxInfluences=False, skinMethod=0, name='glbSkin')[0]
            created.append(cluster)
            cmds.setAttr(cluster+'.geomMatrix', *list(root_matrix),type='matrix')
            selection = om.MSelectionList(); selection.add(cluster)
            fn = oma.MFnSkinCluster(selection.getDependNode(0))
            objects = fn.influenceObjects()
            mapping = {p.fullPathName(): i for i,p in enumerate(objects)}
            logical = {p.fullPathName(): fn.indexForInfluenceObject(p) for p in objects}
            for name, matrix in zip(influences, matrices):
                bind = root_matrix.inverse()*om.MMatrix(matrix)
                cmds.setAttr(cluster+'.bindPreMatrix[%d]' % logical[name],*list(bind),type='matrix')
            attrs = primitive['attributes']
            joints = glb.accessor(attrs['JOINTS_0']); weights = glb.accessor(attrs['WEIGHTS_0'])
            component_fn = om.MFnSingleIndexedComponent()
            component = component_fn.create(om.MFn.kMeshVertComponent)
            component_fn.addElements(range(len(joints)))
            flat = []
            for js,ws in zip(joints,weights):
                row = [0.0]*len(objects)
                total = sum(ws)
                for j,w in zip(js,ws): row[mapping[influences[j]]] += w/total
                flat.extend(row)
            selection = om.MSelectionList(); selection.add(shape)
            fn.setWeights(selection.getDagPath(0),component,om.MIntArray(range(len(objects))),om.MDoubleArray(flat),False)
            cmds.setAttr(cluster+'.normalizeWeights', 1)
