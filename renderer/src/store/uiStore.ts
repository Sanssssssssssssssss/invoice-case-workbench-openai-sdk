import { create } from 'zustand'
import type { TraceEvent } from '@/types'

interface UiState {
  selectedCaseId: string
  selectedEvent: TraceEvent | null
  inspectorTab: string
  setSelectedCaseId: (caseId: string) => void
  setSelectedEvent: (event: TraceEvent | null) => void
  setInspectorTab: (tab: string) => void
}

export const useUiStore = create<UiState>((set) => ({
  selectedCaseId: '',
  selectedEvent: null,
  inspectorTab: 'requirements',
  setSelectedCaseId: (caseId) => set({ selectedCaseId: caseId, selectedEvent: null }),
  setSelectedEvent: (event) => set({ selectedEvent: event }),
  setInspectorTab: (tab) => set({ inspectorTab: tab })
}))
