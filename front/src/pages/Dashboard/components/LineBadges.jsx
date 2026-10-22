import { lineColors } from "../dashboardData.js";

// 노선 번호표. 호선이 없는 계량기는 표시하지 않는다.
export default function LineBadges({ lines }) {
  return (
    <span className="line-badges">
      {lines.filter(Boolean).map((line) => (
        <span className="line-badge" style={{ "--line-color": lineColors[line] || "#667085" }} key={line} aria-label={`${line}호선`}>
          {line}
        </span>
      ))}
    </span>
  );
}
