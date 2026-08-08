import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-24 text-center">
      <div className="text-3xl font-semibold text-content-faint">404</div>
      <p className="text-sm text-content-muted">That page does not exist.</p>
      <Link to="/" className="btn btn-primary">
        Back to dashboard
      </Link>
    </div>
  )
}
