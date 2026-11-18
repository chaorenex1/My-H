(type_declaration (type_spec name: (type_identifier) @symbol.name type: (_) @symbol.type) @symbol.def)
(function_declaration name: (identifier) @symbol.name) @symbol.def
(method_declaration name: (field_identifier) @symbol.name) @symbol.def
(const_spec (identifier) @symbol.name) @symbol.def
(var_spec (identifier) @symbol.name) @symbol.def
(type_declaration (type_spec name: (type_identifier) @symbol.container type: (struct_type (field_declaration_list (field_declaration name: (field_identifier) @symbol.name)))))
(type_declaration (type_spec name: (type_identifier) @symbol.container type: (interface_type (method_elem name: (field_identifier) @symbol.name))))
