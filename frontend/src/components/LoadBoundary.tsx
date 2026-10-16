import { Component, type ReactNode } from 'react';
import { Button } from './ui/button';

/** React.lazy retains rejected imports. Recovery deliberately reloads the page
 * instead of resetting the same failed promise or automatically looping. */
export class LoadBoundary extends Component<{
  children: ReactNode;
  resetKey?: string;
  section?: string;
  homeHref?: string;
}, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidUpdate(previous: Readonly<typeof this.props>) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }
  render() {
    if (!this.state.failed) return this.props.children;
    const Container = this.props.homeHref ? 'main' : 'section';
    const Heading = this.props.homeHref ? 'h1' : 'h2';
    return <Container className="mx-auto max-w-xl space-y-4 px-6 py-12">
      <Heading className="text-xl font-semibold">{this.props.section || 'This page'} could not load</Heading>
      <p role="alert">A required part of the page was unavailable. Reload the page to try again.</p>
      {this.props.section && <p className="text-sm">You can continue using the rest of this page.</p>}
      <p className="text-sm text-muted-foreground">Saved changes remain on your account. Reloading discards unsaved edits.</p>
      <div className="flex flex-wrap gap-3">
        <Button onClick={() => window.location.reload()}>Reload page</Button>
        {this.props.homeHref && <Button asChild variant="outline"><a href={this.props.homeHref}>Return to your workspace</a></Button>}
      </div>
    </Container>;
  }
}
