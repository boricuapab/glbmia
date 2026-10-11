"""Drag this file into Maya's viewport to install GLBMIA for the current user."""
from pathlib import Path
import shutil

FILES = ('glbImport.py', 'glbExport.py', 'glbRoundtrip.py')


def install(destination=None, load=True):
    source = Path(__file__).resolve().parent
    for name in FILES:
        if not (source / name).is_file():
            raise RuntimeError('Missing package file: '+name)
    if destination is None:
        import maya.cmds as cmds
        destination = Path(cmds.internalVar(userAppDir=True)) / 'modules'
    destination = Path(destination)
    plugin_dir = destination / 'glbmia' / 'plug-ins'
    if load:
        import maya.cmds as cmds
        if cmds.pluginInfo('glbImport.py', query=True, loaded=True):
            cmds.unloadPlugin('glbImport.py')
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(str(source / name), str(plugin_dir / name))
    (destination / 'glbmia.mod').write_text(
        '+ glbmia 1.3.0 ./glbmia\nPYTHONPATH +:= plug-ins\n', encoding='utf-8')
    if load:
        import sys
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        cmds.loadPlugin(str(plugin_dir / 'glbImport.py'))
        cmds.pluginInfo('glbImport.py', edit=True, autoload=True)
        cmds.confirmDialog(title='GLBMIA installed',
                           message='Installed and enabled. Auto load is on.\nUse the GLB menu or File > Import.', button=['OK'])
    return plugin_dir


def onMayaDroppedPythonFile(*args):
    install()


if __name__ == '__main__':
    install()
