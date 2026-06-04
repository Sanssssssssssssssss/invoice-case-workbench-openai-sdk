import { Maximize2, Minus, Square, X } from 'lucide-react'

export function TitleBar() {
  const control = (action: 'minimize' | 'maximize' | 'close') => {
    void window.cockpit?.windowControl(action)
  }

  return (
    <header className="titlebar">
      <div className="titlebar-brand">
        <div className="app-mark">IC</div>
        <span>Agent 案件驾驶舱</span>
      </div>
      <div className="titlebar-spacer" />
      <button className="window-button" onClick={() => control('minimize')} aria-label="最小化">
        <Minus size={15} />
      </button>
      <button className="window-button" onClick={() => control('maximize')} aria-label="最大化">
        <Square size={13} />
      </button>
      <button className="window-button close" onClick={() => control('close')} aria-label="关闭">
        <X size={16} />
      </button>
    </header>
  )
}
