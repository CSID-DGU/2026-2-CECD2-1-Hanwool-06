// 여러 화면이 함께 쓰는 작은 UI 조각.

export function SectionTitle({ children, right, as: Heading = "h3" }) {
  return (
    <div className="section-title">
      <Heading>{children}</Heading>
      {right ? <div className="section-title-right">{right}</div> : null}
    </div>
  );
}

export function Card({ title, right, children, className = "" }) {
  return (
    <section className={`dt-card ${className}`}>
      {title ? <SectionTitle as="h2" right={right}>{title}</SectionTitle> : null}
      {children}
    </section>
  );
}

export function Stat({ label, value }) {
  return (
    <div className="dt-stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function EmptyNote({ children }) {
  return <p className="empty-state">{children}</p>;
}
