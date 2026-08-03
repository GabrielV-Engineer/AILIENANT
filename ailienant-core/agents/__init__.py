# Package marker — required for mypy's explicit_package_bases resolution across
# this flat-but-packaged layout (see mypy.ini), which disambiguates same-basename
# modules living in sibling packages (e.g. api.audit vs core.audit).
