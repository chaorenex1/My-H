export * from "./catalog.ts";
export * from "./coercion.ts";
export * from "./demotion.ts";
export * from "./examples.ts";
export * from "./factory.ts";
export * from "./history.ts";
export * from "./inventory.ts";
export * from "./owned-stream.ts";
// `./rendering` is a dialect-internal primitives module deliberately excluded
// from the barrel. `renderDelimitedThinking` is the one helper an external
// consumer needs (the legacy markdown `/dump` reuses its `<thinking>` envelope
// unwrap), so re-export only that symbol rather than `export *`-ing the rest.
export { renderDelimitedThinking } from "./rendering.ts";
export * from "./thinking.ts";
export * from "./types.ts";
