import { motion } from 'framer-motion'
import type { Requirement } from '@/types'
import { requirementProgress, requirementSegments } from '@/lib/requirements'

export function RequirementRing({ requirements, size = 88, onClick }: { requirements: Requirement[]; size?: number; onClick?: () => void }) {
  const progress = requirementProgress(requirements)
  const segments = requirementSegments(requirements)
  const radius = (size - 14) / 2
  const circumference = 2 * Math.PI * radius
  let offset = 0

  return (
    <button className="requirement-ring" style={{ width: size, height: size }} onClick={onClick} aria-label="打开需求">
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        <circle className="ring-track" cx={size / 2} cy={size / 2} r={radius} strokeWidth="8" />
        {segments.map((segment) => {
          const dash = segment.value * circumference
          const strokeDasharray = `${dash} ${circumference - dash}`
          const strokeDashoffset = -offset
          offset += dash
          return (
            <motion.circle
              key={segment.key}
              cx={size / 2}
              cy={size / 2}
              r={radius}
              stroke={segment.color}
              strokeWidth="8"
              strokeLinecap="round"
              fill="none"
              initial={{ strokeDasharray: `0 ${circumference}` }}
              animate={{ strokeDasharray, strokeDashoffset }}
              transition={{ duration: 0.45, ease: 'easeOut' }}
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
            />
          )
        })}
      </svg>
      <span className="ring-percent">{progress.percent}%</span>
      <span className="ring-caption">{progress.ready}/{progress.total} 就绪</span>
    </button>
  )
}
