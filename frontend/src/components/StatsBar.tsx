import { Separator } from './ui/separator';
import { Progress } from './ui/progress';
import { CheckCircle2, Link2 } from 'lucide-react';

interface StatsBarProps {
  stats: {
    total: number;
    exact: number;
    high: number;
    medium: number;
    low: number;
    approved: number;
    approvalProgress: number;
  };
}

export function StatsBar({ stats }: StatsBarProps) {
  return (
    <div className="bg-card border border-border rounded-md p-4 flex items-center gap-6 flex-wrap">
      {/* Total */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">Total Pages</span>
        <span className="text-foreground font-medium">{stats.total}</span>
      </div>

      <Separator orientation="vertical" className="h-6" />

      {/* Confidence breakdown */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <Link2 className="w-3.5 h-3.5 text-blue-500" />
          <span className="text-sm text-muted-foreground">Exact</span>
          <span className="text-sm text-foreground font-medium">{stats.exact}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2.5 h-2.5 bg-green-500 rounded-full" />
          <span className="text-sm text-muted-foreground">High</span>
          <span className="text-sm text-foreground font-medium">{stats.high}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2.5 h-2.5 bg-yellow-500 rounded-full" />
          <span className="text-sm text-muted-foreground">Medium</span>
          <span className="text-sm text-foreground font-medium">{stats.medium}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2.5 h-2.5 bg-red-500 rounded-full" />
          <span className="text-sm text-muted-foreground">Low</span>
          <span className="text-sm text-foreground font-medium">{stats.low}</span>
        </div>
      </div>

      <Separator orientation="vertical" className="h-6" />

      {/* Approval progress */}
      <div className="flex items-center gap-3 flex-1 min-w-[200px]">
        <CheckCircle2 className="h-4 w-4 text-green-600 flex-shrink-0" />
        <span className="text-sm text-muted-foreground whitespace-nowrap">
          {stats.approved}/{stats.total} approved
        </span>
        <Progress aria-label="Redirect approval progress" aria-valuetext={`${stats.approved} of ${stats.total} approved`} value={stats.approvalProgress} className="h-2 flex-1" />
        <span className="text-sm text-foreground font-medium whitespace-nowrap">
          {stats.approvalProgress}%
        </span>
      </div>
    </div>
  );
}
