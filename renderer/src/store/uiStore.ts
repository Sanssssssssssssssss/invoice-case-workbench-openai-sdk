import { create } from 'zustand'
import type { TraceEvent } from '@/types'

interface UiState {
  selectedCaseId: string
  selectedRunId: string
  selectedEvent: TraceEvent | null
  inspectorTab: string
  setSelectedCaseId: (caseId: string) => void
  setSelectedRunId: (runId: string) => void
  setSelectedEvent: (event: TraceEvent | null) => void
  setInspectorTab: (tab: string) => void
}

export const useUiStore = create<UiState>((set) => ({
  selectedCaseId: '',
  selectedRunId: '',
  selectedEvent: null,
  inspectorTab: 'trace',
  setSelectedCaseId: (caseId) => set({ selectedCaseId: caseId, selectedRunId: '', selectedEvent: null }),
  setSelectedRunId: (runId) => set({ selectedRunId: runId, selectedEvent: null }),
  setSelectedEvent: (event) => set({ selectedEvent: event }),
  setInspectorTab: (tab) => set({ inspectorTab: tab })
}))
