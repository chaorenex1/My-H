(struct_item name: (type_identifier) @symbol.name) @symbol.def
(union_item name: (type_identifier) @symbol.name) @symbol.def
(enum_item name: (type_identifier) @symbol.name) @symbol.def
(trait_item name: (type_identifier) @symbol.name) @symbol.def
(mod_item name: (identifier) @symbol.name) @symbol.def
(function_item name: (identifier) @symbol.name) @symbol.def
(function_signature_item name: (identifier) @symbol.name) @symbol.def
(const_item name: (identifier) @symbol.name) @symbol.def
(static_item name: (identifier) @symbol.name) @symbol.def
(type_item name: (type_identifier) @symbol.name) @symbol.def
(impl_item type: (_) @symbol.container body: (declaration_list (function_item name: (identifier) @symbol.name) @symbol.def))
(trait_item name: (type_identifier) @symbol.container body: (declaration_list (function_signature_item name: (identifier) @symbol.name) @symbol.def))
(struct_item name: (type_identifier) @symbol.container body: (field_declaration_list (field_declaration name: (field_identifier) @symbol.name) @symbol.def))
(enum_item name: (type_identifier) @symbol.container body: (enum_variant_list (enum_variant name: (identifier) @symbol.name) @symbol.def))
