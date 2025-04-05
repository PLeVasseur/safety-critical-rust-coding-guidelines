
.. SPDX-License-Identifier: MIT OR Apache-2.0
   SPDX-FileCopyrightText: The Coding Guidelines Subcommittee Contributors

.. default-domain:: coding-guidelines

###############
Style Guideline
###############

*****************************
Example of a coding guideline
*****************************

Below is an example of a coding guideline.

We will examine each part:

* ``guideline``
* ``rationale``
* ``non_compliant_example``
* ``compliant_example``

::

   .. guideline:: Avoid Implicit Integer Wrapping
      :id: gui_xztNdXA2oFNB
      :category: required
      :status: draft
      :fls: fls_cokwseo3nnr
      :decidability: decidable
      :scope: module
      :tags: numerics

      Code must not rely on Rust's implicit integer wrapping behavior that occurs in release builds. 
      Instead, explicitly handle potential overflows using the standard library's checked, 
      saturating, or wrapping operations.

      .. rationale::
         :id: rat_kYiIiW8R2qD1
         :status: draft

         In debug builds, Rust performs runtime checks for integer overflow and will panic if detected.
         However, in release builds (with optimizations enabled), integer operations silently wrap
         around on overflow, creating potential for silent failures and security vulnerabilities.
         
         Safety-critical software requires consistent and predictable behavior across all build
         configurations. Explicit handling of potential overflow conditions improves code clarity,
         maintainability, and reduces the risk of numerical errors in production.

      .. non_compliant_example::
         :id: non_compl_ex_PO5TyFsRTlWv
         :status: draft
      
          .. code-block:: rust
      
            fn calculate_next_position(current: u32, velocity: u32) -> u32 {
                // Potential for silent overflow in release builds
                current + velocity
            }

      .. compliant_example::
         :id: compl_ex_WTe7GoPu5Ez0
         :status: draft
      
          .. code-block:: rust
      
            fn calculate_next_position(current: u32, velocity: u32) -> u32 {
                // Explicitly handle potential overflow with checked addition
                current.checked_add(velocity).expect("Position calculation overflowed")
            }

``guideline``
=============


::

   .. guideline:: Avoid Implicit Integer Wrapping
      :id: gui_xztNdXA2oFNB
      :category: required
      :status: draft
      :fls: fls_cokwseo3nnr
      :decidability: decidable
      :scope: module
      :tags: numerics

``id``
------

A unique identifier for each guideline. Guideline identifiers begin with ``gui_``.

These identifiers are considered **stable** across releases and will never be removed.
See ``status`` below for more.

Can be generated using the ``generate-guideline-templates.py`` script.


``category``
------------

Four possible values, semantics of each differ:

* ``mandatory``
* ``required``
* ``advisory``
* ``disapplied``

``mandatory``
^^^^^^^^^^^^^

Code claimed to be in compliance with this document must follow every guideline marked as ``mandatory``.

*TODO(pete.levasseur): Add more tips on when this is a good choice for a guideline.*

``required``
^^^^^^^^^^^^

Code claimed to be in compliance with this document shall follow every guideline marked as ``required``,
with a formal deviation required as outlined in :ref:`Compliance`, where this is not the case.

An organization or project may choose to recategorize any ``required`` guideline to ``mandatory``.

*TODO(pete.levasseur): Add more tips on when this is a good choice for a guideline.*

``advisory``
^^^^^^^^^^^^

These are recommendations. However, the category of ``advisory`` does not mean that these items can
be ignored, but rather that they should be followed as far as reasonably practical. Formal deviation
is not necessary for advisory guidelines but, if the formal deviation process is not followed,
alternative arrangements should be made for documenting non-compliances.

An organization or project may choose to recategorize any ``advisory`` guideline as ``mandatory``
or ``required``, or as ``disapplied``.

*TODO(pete.levasseur): Add more tips on when this is a good choice for a guideline.*

``disapplied``
^^^^^^^^^^^^^^

These are guidelines for which compliance is not required. No enforcement is expected, and any
non-compliance may be disregarded.

*Note*: Where a guideline does not apply to the chosen release of the Rust compiler, it is treated
as ``disapplied`` for the purposes of coding guideline :ref:`Compliance`.

An organization or project may choose to recategorize any ``disapplied`` guideline as ``mandatory``
or ``required``, or as ``advisory``.

*TODO(pete.levasseur): Add more tips on when this is a good choice for a guideline.*

``status``
----------

Four possible values, semantics of each differ:

* ``draft``
* ``proposed``
* ``approved``
* ``deprecated``


