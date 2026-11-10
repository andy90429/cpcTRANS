from pathlib import Path


project = Path(SPECPATH)
analysis = Analysis(
    [str(project / "fuelcore" / "__main__.py")],
    pathex=[str(project)],
    datas=[(str(project / "fuelcore" / "schema.csv"), "fuelcore"), (str(project / "pyproject.toml"), ".")],
)
modules = PYZ(analysis.pure)
utf8 = ("X utf8", None, "OPTION")
gui_flag = ("X fuelcore_gui", None, "OPTION")
gui = EXE(modules, analysis.scripts, [utf8, gui_flag], exclude_binaries=True,
          name="fuelcore", console=False)
cli = EXE(modules, analysis.scripts, [utf8], exclude_binaries=True,
          name="fuelcore_cli", console=True)
bundle = COLLECT(gui, cli, analysis.binaries, analysis.datas, name="fuelcore")
