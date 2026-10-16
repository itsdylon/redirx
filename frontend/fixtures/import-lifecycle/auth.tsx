import { createContext, useContext } from 'react';
export const FixtureOwner = createContext('fixture-owner-a');
export function useAuth() { return { user: { id: useContext(FixtureOwner), plan: 'agency', email: 'fixture@example.com' } }; }
