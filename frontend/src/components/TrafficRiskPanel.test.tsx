import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { TrafficRiskPanel } from './TrafficRiskPanel';
import { computeRisk } from '../lib/riskSummary';
afterEach(cleanup);
it('shows impressions for impression-weighted priorities instead of zero-click rows',()=>{
 const risk=computeRisk('example.test',[{url:'https://example.test/a',sources:['gsc'],clicks:0,impressions:1200}],undefined);
 render(<TrafficRiskPanel risk={risk}/>);
 expect(screen.getByText('1,200',{selector:'li span'})).toBeInTheDocument();
 expect(screen.getByText('impressions',{selector:'li span'})).toBeInTheDocument();
 expect(screen.queryByText('clicks',{selector:'li span'})).not.toBeInTheDocument();
 expect(screen.getByText(/at least 80% of recorded organic impressions/)).toBeInTheDocument();
});
it('does not predict traffic loss when measurement is absent',()=>{
 render(<TrafficRiskPanel risk={computeRisk('example.test',[],undefined)} />);
 expect(screen.getByText(/Search traffic has not been measured/)).toBeInTheDocument();
 expect(screen.queryByText(/about to break/)).not.toBeInTheDocument();
});
