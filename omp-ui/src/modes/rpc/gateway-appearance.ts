/**
 * The omp-ui Appearance panel is profile state, not shell state.
 *
 * Every setting rendered by the **Appearance** tab is an ordinary Hermes config key under one
 * section, loaded from the paired gateway (`omp.appearance.get`) and written back to
 * `<HERMES_HOME>/config.yaml`:
 *
 * ```yaml
 * omp:
 *   appearance:
 *     theme:
 *       dark: titanium
 *     symbolPreset: nerd
 * ```
 *
 * The gateway answers every setting, filling unset ones from omp's own registry defaults
 * (`DEFAULT_CONFIG`), so the shell never has to guess a missing value. Boot: values arrive as
 * runtime setting overrides, so they stay in effect above the shell's own `config.yml` without
 * being persisted there. Edits: the panel's `Setting.set`/`unset` writes are redirected to the
 * runtime override (immediate, synchronous) plus `omp.appearance.set` (the gateway writes
 * `config.yaml`), so the shell never owns a second copy of appearance state.
 *
 * The redirect patches `Setting.prototype`, not `config/settings-ui.ts`: `src/` is a byte-identical
 * upstream snapshot (`VENDORED.md`), and this module lives outside it.
 */

import type { GatewayClient } from "./gateway-client"
import { liveGatewayClient } from "./gateway-live-client"
import { Setting, lookup, type AnySetting, type ScopeLike } from "../../config/registry"
import type { Settings } from "../../config/settings"

export const APPEARANCE_TAB = "appearance"

/** Setting ids (dotted, e.g. `theme.dark`) → their stored JSON values. */
export type AppearanceValues = Record<string, unknown>

export interface AppearancePersistenceOptions {
  settings: Settings
  client: GatewayClient
  /** A write that did not reach `config.yaml`; the shell keeps the value for this run. */
  onError?: (error: Error) => void
}

/** Read the profile's appearance section; the server flattens it to dotted setting ids. */
export async function loadGatewayAppearance(client: GatewayClient): Promise<AppearanceValues> {
  const result = await client.request("omp.appearance.get", {})
  return (result.values ?? {}) as AppearanceValues
}

/**
 * Apply stored values as runtime overrides, before the shell reads them (theme bootstrap included).
 * An id this build does not render — or a value it rejects — is skipped instead of aborting boot,
 * so a config written by a newer/older shell cannot brick the terminal.
 */
export function applyGatewayAppearance(settings: Settings, values: AppearanceValues): string[] {
  const applied: string[] = []
  for (const [id, value] of Object.entries(values)) {
    const setting = lookup(id)
    if (!setting || setting.ui?.tab !== APPEARANCE_TAB) continue
    try {
      setting.override(settings, value)
      applied.push(id)
    } catch {
      // Stale or hand-edited value: drop the override (an effect may have thrown after it landed)
      // so this setting keeps its default/global value instead of booting into a broken value.
      setting.override(settings, undefined)
    }
  }
  return applied
}

interface AppearanceWriteThrough {
  write(setting: AnySetting, value: unknown): void
  unset(setting: AnySetting): void
}

interface SettingPrototype {
  set(this: AnySetting, scope: ScopeLike, value: unknown): void
  unset(this: AnySetting, scope: ScopeLike): void
}

let activeWriteThrough: AppearanceWriteThrough | undefined
let prototypePatched = false

export interface AppearancePersistence {
  /** Stop routing Appearance writes to the gateway (the shell keeps running on the overrides). */
  uninstall(): void
  /** Resolves once every gateway write queued so far has settled (teardown/tests). */
  flush(): Promise<void>
}

/** Route Appearance-tab persistence to the gateway; returns the handle (teardown, tests). */
export function installGatewayAppearancePersistence(options: AppearancePersistenceOptions): AppearancePersistence {
  const { settings, client, onError } = options
  let writes = Promise.resolve()

  const send = (key: string, value: unknown): void => {
    writes = writes
      .then(async () => {
        await liveGatewayClient(client).request("omp.appearance.set", { key, value: value ?? null })
      })
      .catch((error: unknown) => {
        onError?.(error instanceof Error ? error : new Error(String(error)))
      })
  }

  const sink: AppearanceWriteThrough = {
    write(setting, value) {
      setting.override(settings, value)
      send(setting.id, value === undefined ? null : setting.get(settings))
    },
    unset(setting) {
      setting.override(settings, undefined)
      send(setting.id, null)
    },
  }
  activeWriteThrough = sink
  patchSettingPrototype()
  return {
    uninstall() {
      if (activeWriteThrough === sink) activeWriteThrough = undefined
    },
    flush: () => writes,
  }
}

function patchSettingPrototype(): void {
  if (prototypePatched) return
  prototypePatched = true
  const proto = Setting.prototype as unknown as SettingPrototype
  const originalSet = proto.set
  const originalUnset = proto.unset
  proto.set = function (this: AnySetting, scope: ScopeLike, value: unknown): void {
    if (activeWriteThrough && this.ui?.tab === APPEARANCE_TAB) {
      activeWriteThrough.write(this, value)
      return
    }
    originalSet.call(this, scope, value)
  }
  proto.unset = function (this: AnySetting, scope: ScopeLike): void {
    if (activeWriteThrough && this.ui?.tab === APPEARANCE_TAB) {
      activeWriteThrough.unset(this)
      return
    }
    originalUnset.call(this, scope)
  }
}
