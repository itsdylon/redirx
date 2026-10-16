import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReviewInterface, type RedirectMapping } from './ReviewInterface';
import { queryKeys } from '../queries/queryKeys';
const mocks = vi.hoisted(() => ({ auth: vi.fn(), results: vi.fn(), save: vi.fn(), success: vi.fn() }));
vi.mock('../contexts/AuthContext', () => ({ useAuth: mocks.auth }));
vi.mock('../api/pipeline', () => ({ getResults: mocks.results, persistReviewDecision: mocks.save, getDeepPreview: vi.fn() }));
vi.mock('../api/billing', () => ({ getProjectUnlockStatus: vi.fn() }));
vi.mock('@posthog/react', () => ({ usePostHog: () => null }));
vi.mock('react-hotkeys-hook', () => ({ useHotkeys: vi.fn() }));
vi.mock('sonner', () => ({ toast: { success: mocks.success, info: vi.fn() } }));
vi.mock('../contexts/OnboardingContext', () => ({ useOnboarding: () => ({onboarding:null, isStepCompleted:()=>false}) }));
vi.mock('./ToolLayout', () => ({ToolLayout: ({children}: any) => <div>{children}</div>}));
vi.mock('./StatsBar', () => ({StatsBar: () => null}));
vi.mock('./GscTrafficCard', () => ({GscTrafficCard: ({gscMeta}: any) => <p>{gscMeta?.property}</p>}));
vi.mock('./WatchPrompt', () => ({WatchPrompt:()=>null}));
vi.mock('./ReviewToolbar', () => ({ReviewToolbar: ({onExportClick,exportDisabled,searchQuery,onSearchChange}:any) => <><input aria-label="Search redirects" value={searchQuery} onChange={event=>onSearchChange(event.target.value)} /><button onClick={onExportClick} disabled={exportDisabled}>Export</button></>}));
vi.mock('./ExportModal', () => ({ExportModal: ({open, redirects}:any) => open ? <pre data-testid="export">{JSON.stringify(redirects)}</pre>:null}));
vi.mock('./RedirectTable', () => ({RedirectTable: ({redirects,onApprove,onEdit,onToggleSelect,selectedRows,actionsDisabled}:any) => <div>{redirects.map((row:RedirectMapping)=><div key={row.id} data-testid={row.id}>
  <span>{row.newUrl} {row.approved?'approved':'unapproved'}</span>
  <button disabled={actionsDisabled} onClick={()=>onApprove(row.id)}>Approve {row.id}</button>
  <button onClick={()=>onEdit(row)}>Edit {row.id}</button>
  <button aria-pressed={selectedRows.has(row.id)} onClick={()=>onToggleSelect(row.id)}>Select {row.id}</button>
</div>)}</div>}));
vi.mock('./InlineEditDialog', () => ({InlineEditDialog: ({redirect,onSave,onCancel}:any) => <div role="dialog"><button onClick={()=>void onSave({...redirect,newUrl:'/edited'}).catch(()=>{})}>Save draft</button><button onClick={onCancel}>Cancel draft</button></div>}));
vi.mock('./ui/select', () => ({ Select: ({onValueChange,disabled,children}:any) => <select aria-label="Bulk Actions" disabled={disabled} value="" onChange={e=>onValueChange(e.target.value)}><option value="" />{children}</select>,SelectTrigger:()=>null,SelectValue:()=>null,SelectContent:({children}:any)=><>{children}</>,SelectItem:({value,children}:any)=><option value={value}>{children}</option> }));
const row = (id='row-1', approved=false):RedirectMapping => ({id,oldUrl:'/old-'+id,newUrl:'/new-'+id,approved,confidence:90,confidenceBand:'high',matchScore:90,warnings:[],pathSimilarity:90,titleSimilarity:90,contentSimilarity:90});
const result = (rows=[row()], property='owner-a property') => ({success:true,mappings:rows,session:{pipeline_type:'content'},gsc:{synced:true,property}});
function deferred<T>() { let resolve!:(value:T)=>void; let reject!:(error:Error)=>void;const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject}; }
function mount(client = new QueryClient({defaultOptions:{queries:{retry:false}}})) {
  const tree = <QueryClientProvider client={client}><MemoryRouter initialEntries={['/review/session']}><Routes><Route path="/review/:sessionId" element={<ReviewInterface layoutVariant="tool" pivotEnabled />} /></Routes></MemoryRouter></QueryClientProvider>;
  return {...render(tree), client, tree};
}
beforeEach(()=>{vi.clearAllMocks(); mocks.auth.mockReturnValue({user:{id:'owner-a',plan:'free'}});mocks.results.mockResolvedValue(result());mocks.save.mockResolvedValue(undefined);});
describe('review persistence and ownership',()=>{
  it('does not approve or permit export before acknowledgment, then updates the owned cache',async()=>{
    const pending=deferred<void>();mocks.save.mockReturnValue(pending.promise);const view=mount();
    await screen.findByTestId('row-1'); await userEvent.click(screen.getByText('Approve row-1'));
    expect(screen.getByTestId('row-1')).toHaveTextContent('unapproved');expect(screen.getByText('Export')).toBeDisabled();expect(mocks.success).not.toHaveBeenCalled();
    await act(async()=>pending.resolve());
    expect(screen.getByTestId('row-1')).toHaveTextContent('/new-row-1 approved');
    expect((view.client.getQueryData(queryKeys.results.forOwner('owner-a','session')) as any).mappings[0].approved).toBe(true);
    expect(mocks.success).toHaveBeenCalledWith('1 review change saved');
  });
  it('retains last confirmed decisions after failure and requires readback before export',async()=>{
    mocks.save.mockRejectedValue(new Error('Network interrupted'));mount();await screen.findByTestId('row-1');await userEvent.click(screen.getByText('Approve row-1'));
    expect(await screen.findByRole('alert')).toHaveTextContent('not confirmed');expect(screen.getByTestId('row-1')).toHaveTextContent('unapproved');expect(screen.getByText('Export')).toBeDisabled();expect(mocks.save).toHaveBeenCalledTimes(1);
    // The backend may have committed despite the failed response.
    mocks.results.mockResolvedValue(result([row('row-1',true)]));await userEvent.click(screen.getByText('Reload results'));
    await waitFor(()=>expect(screen.getByText('Export')).toBeEnabled());expect(screen.getByTestId('row-1')).toHaveTextContent('/new-row-1 approved');
  });
  it('clears approval when a changed target is saved, preserving model evidence',async()=>{
    mocks.results.mockResolvedValue(result([row('row-1',true)]));mount();await screen.findByTestId('row-1');await userEvent.click(screen.getByText('Edit row-1'));await userEvent.click(screen.getByText('Save draft'));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());expect(mocks.save).toHaveBeenCalledWith('session','row-1',{new_url:'/edited',match_type:'manual',approved:false,expected_new_url:'/new-row-1'});expect(screen.getByTestId('row-1')).toHaveTextContent('/edited unapproved');
  });
  it('bounds bulk writes to four, acknowledges only successful rows and keeps failed selection',async()=>{
    mocks.results.mockResolvedValue(result(Array.from({length:9},(_,i)=>row('row-'+i))));const pending=Array.from({length:9},()=>deferred<void>());mocks.save.mockImplementation((_s,id)=>pending[Number(id.split('-')[1])].promise);mount();await screen.findByTestId('row-0');
    await userEvent.selectOptions(screen.getByLabelText('Bulk Actions'),'approve-all-high');expect(mocks.save).toHaveBeenCalledTimes(4);
    await act(async()=>{pending[0].resolve();pending[1].resolve();pending[2].reject(new Error('unknown'));pending[3].resolve();});expect(mocks.save).toHaveBeenCalledTimes(8);
    await act(async()=>{pending[4].resolve();pending[5].resolve();pending[6].resolve();pending[7].resolve();});expect(mocks.save).toHaveBeenCalledTimes(9);
    await act(async()=>pending[8].resolve());expect(mocks.success).toHaveBeenCalledWith('8 review changes saved');expect(screen.getByText('Select row-2')).toHaveAttribute('aria-pressed','true');expect(screen.getByTestId('row-2')).toHaveTextContent('unapproved');
    await userEvent.selectOptions(screen.getByLabelText('Bulk Actions'),'clear-selection');expect(screen.getByText('Select row-2')).toHaveAttribute('aria-pressed','false');
  });
  it('does not hydrate another owner cache or apply late mutation completion after an owner switch',async()=>{
    const pending=deferred<void>();mocks.save.mockReturnValue(pending.promise);const view=mount();await screen.findByTestId('row-1');await userEvent.click(screen.getByText('Approve row-1'));
    mocks.auth.mockReturnValue({user:{id:'owner-b',plan:'free'}});const second=deferred<any>();mocks.results.mockReturnValue(second.promise);view.rerender(<QueryClientProvider client={view.client}><MemoryRouter initialEntries={['/review/session']}><Routes><Route path="/review/:sessionId" element={<ReviewInterface layoutVariant="tool" pivotEnabled />} /></Routes></MemoryRouter></QueryClientProvider>);
    expect(screen.queryByTestId('row-1')).not.toBeInTheDocument();expect(screen.queryByText('owner-a property')).not.toBeInTheDocument();
    await act(async()=>{pending.resolve();second.resolve(result([row('owner-b-row')],'owner-b property'));});expect(await screen.findByTestId('owner-b-row')).toHaveTextContent('unapproved');expect(mocks.success).not.toHaveBeenCalled();expect(view.client.getQueryData(queryKeys.results.forOwner('owner-b','session'))).toMatchObject({mappings:[{id:'owner-b-row',approved:false}]});
  });
  it('stops queued bulk writes when the owner changes, even if the first four finish later', async () => {
    mocks.results.mockResolvedValue(result(Array.from({length:9}, (_,i)=>row('row-'+i))));
    const pending = Array.from({length:4}, ()=>deferred<void>());
    mocks.save.mockImplementation((_session,id)=>pending[Number(id.split('-')[1])].promise);
    const view = mount(); await screen.findByTestId('row-0');
    await userEvent.selectOptions(screen.getByLabelText('Bulk Actions'), 'approve-all-high');
    expect(mocks.save).toHaveBeenCalledTimes(4);
    mocks.auth.mockReturnValue({user:{id:'owner-b',plan:'free'}});
    mocks.results.mockResolvedValue(result([row('owner-b-row')], 'owner-b property'));
    view.rerender(<QueryClientProvider client={view.client}><MemoryRouter initialEntries={['/review/session']}><Routes><Route path="/review/:sessionId" element={<ReviewInterface layoutVariant="tool" pivotEnabled />} /></Routes></MemoryRouter></QueryClientProvider>);
    await screen.findByTestId('owner-b-row');
    await act(async()=>{ pending[0].resolve(); pending[1].reject(new Error('unknown outcome')); pending[2].resolve(); pending[3].resolve(); });
    expect(mocks.save).toHaveBeenCalledTimes(4);
    expect(mocks.success).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('owner-b-row')).toHaveTextContent('unapproved');
    expect(view.client.getQueryData(queryKeys.results.forOwner('owner-b','session'))).toMatchObject({mappings:[{id:'owner-b-row',approved:false}]});
  });

  it('does not unlock export from cached data after a failed save readback', async () => {
    mocks.save.mockRejectedValue(new Error('not confirmed'));
    mount(); await screen.findByTestId('row-1');
    await userEvent.click(screen.getByText('Approve row-1'));
    expect(await screen.findByRole('alert')).toHaveTextContent('not confirmed');
    mocks.results.mockRejectedValue(new Error('Readback unavailable'));
    await userEvent.click(screen.getByText('Reload results'));
    expect(await screen.findByText('Readback unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('export')).not.toBeInTheDocument();
    expect(screen.queryByText('Export')).not.toBeInTheDocument();
    expect(mocks.save).toHaveBeenCalledTimes(1);
    mocks.results.mockResolvedValue(result([row('row-1',true)]));
    await userEvent.click(screen.getByText('Reload results'));
    expect(await screen.findByText('Export')).toBeEnabled();
    expect(screen.getByTestId('row-1')).toHaveTextContent('/new-row-1 approved');
  });

  it('uses the acknowledged edited target for the next approval and owned cache', async () => {
    mocks.results.mockResolvedValue(result([{...row(),persistedNewUrl:'/new-row-1'}]));
    const view = mount(); await screen.findByTestId('row-1');
    await userEvent.click(screen.getByText('Edit row-1')); await userEvent.click(screen.getByText('Save draft'));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await userEvent.click(screen.getByText('Approve row-1'));
    expect(mocks.save).toHaveBeenLastCalledWith('session','row-1',{approved:true,expected_new_url:'/edited'});
    expect((view.client.getQueryData(queryKeys.results.forOwner('owner-a','session')) as any).mappings[0]).toMatchObject({newUrl:'/edited',persistedNewUrl:'/edited',approved:true});
  });

  it('can search an unresolved display and edit using its raw null precondition', async () => {
    mocks.results.mockResolvedValue(result([{...row(),newUrl:'',persistedNewUrl:null}]));
    mount(); await screen.findByTestId('row-1');
    await userEvent.type(screen.getByRole('textbox',{name:'Search redirects'}), 'no-match');
    expect(screen.queryByTestId('row-1')).not.toBeInTheDocument();
    await userEvent.clear(screen.getByRole('textbox',{name:'Search redirects'}));
    await userEvent.click(screen.getByText('Edit row-1')); await userEvent.click(screen.getByText('Save draft'));
    await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mocks.save).toHaveBeenCalledWith('session','row-1',{new_url:'/edited',match_type:'manual',approved:false,expected_new_url:null});
  });

  it('uses the readback target after a conflict instead of retrying the stale approval', async () => {
    mocks.results.mockResolvedValue(result([{...row(),persistedNewUrl:'/new-row-1'}]));
    mocks.save.mockRejectedValueOnce(new Error('review_target_conflict'));
    mount(); await screen.findByTestId('row-1'); await userEvent.click(screen.getByText('Approve row-1'));
    expect(await screen.findByRole('alert')).toHaveTextContent('not confirmed');
    expect(screen.getByText('Export')).toBeDisabled(); expect(mocks.save).toHaveBeenCalledTimes(1);
    mocks.results.mockResolvedValue(result([{...row(),newUrl:'/other-tab',persistedNewUrl:'/other-tab'}]));
    await userEvent.click(screen.getByText('Reload results'));
    await waitFor(()=>expect(screen.getByText('Export')).toBeEnabled());
    expect(screen.getByTestId('row-1')).toHaveTextContent('/other-tab unapproved');
    await userEvent.click(screen.getByText('Approve row-1'));
    expect(mocks.save).toHaveBeenLastCalledWith('session','row-1',{approved:true,expected_new_url:'/other-tab'});
  });

});
