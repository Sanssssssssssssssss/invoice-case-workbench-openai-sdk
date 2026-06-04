import type { CockpitApi } from '../../desktop/src/preload'

declare global {
  interface Window {
    cockpit?: CockpitApi
  }
}

export {}
