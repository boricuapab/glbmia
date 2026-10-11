"""Drag into Maya to install the single glbformaya plug-in."""
from pathlib import Path
import shutil
import sys

FILES = ('plug-ins/glbformaya.py', 'scripts/glbformaya_support/__init__.py',
         'scripts/glbformaya_support/static_export.py', 'scripts/glbformaya_support/roundtrip.py')


def install(destination=None, load=True, notify=True):
    source = Path(__file__).resolve().parent
    for name in FILES:
        if not (source/name).is_file():
            raise RuntimeError('Incomplete package: '+name)
    if destination is None:
        import maya.cmds as cmds
        destination = Path(cmds.internalVar(userAppDir=True))/'modules'
    destination = Path(destination).resolve()
    package = destination/'glbformaya'
    plugin_dir = package/'plug-ins'
    if load:
        import maya.cmds as cmds
        for name in ('glbImport.py','glbformaya.py'):
            if cmds.pluginInfo(name, query=True, loaded=True):
                cmds.pluginInfo(name, edit=True, autoload=False)
                cmds.unloadPlugin(name)
    for name in FILES:
        target = package/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(str(source/name),str(target))
    (destination/'glbformaya.mod').write_text('+ glbformaya 1.4.2 ./glbformaya\n',encoding='utf-8')
    # Archive only recognized files from our old layout, outside plug-in search folders.
    legacy_dirs = [destination/'glbmia'/'plug-ins']
    if load:
        legacy_dirs.append(Path(cmds.internalVar(userAppDir=True))/cmds.about(version=True)/'plug-ins')
    signatures = {'glbImport.py':'class GLBTranslator(', 'glbExport.py':'def _fast64_extras(',
                  'glbRoundtrip.py':'def export_imported_glb('}
    for index, folder in enumerate(legacy_dirs):
        for name, signature in signatures.items():
            old = folder/name
            if old.is_file() and signature in old.read_text(encoding='utf-8'):
                backup = package/'legacy_backup'/str(index)/name
                backup.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(str(old),str(backup))
                old.unlink()
    old_mod = destination/'glbmia.mod'
    if old_mod.is_file() and '+ glbmia ' in old_mod.read_text(encoding='utf-8'):
        old_mod.unlink()
    if load:
        for folder in (plugin_dir,package/'scripts'):
            if str(folder) not in sys.path: sys.path.insert(0,str(folder))
        for name in list(sys.modules):
            if name == 'glbformaya' or name.startswith('glbformaya_support'):
                del sys.modules[name]
        cmds.loadPlugin(str(plugin_dir/'glbformaya.py'))
        cmds.pluginInfo('glbformaya.py',edit=True,autoload=True)
        if notify:
            cmds.confirmDialog(title='glbformaya installed',message='Installed and enabled. Auto load is on.',button=['OK'])
    return plugin_dir


def onMayaDroppedPythonFile(*args):
    install()


if __name__ == '__main__':
    install()
