# API reference

Reference documentation for the public `fastpg` API, generated from its docstrings.

::: fastpg
    options:
      show_root_heading: true
      show_source: false
      # Connection and Transaction share their query methods through a private base class.
      inherited_members:
        - select_one
        - select_one_or_none
        - select_many
        - select_and_group
        - execute
        - execute_script
        - execute_many
        - insert_many
        - copy_in
