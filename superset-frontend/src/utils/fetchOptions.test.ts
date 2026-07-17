/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import { SupersetClient } from '@superset-ui/core';
import { fetchPaginatedData } from './fetchOptions';

jest.mock('@superset-ui/core', () => ({
  ...jest.requireActual('@superset-ui/core'),
  SupersetClient: {
    get: jest.fn(),
  },
}));

const mockGet = SupersetClient.get as jest.Mock;

const baseArgs = {
  endpoint: '/api/v1/security/roles/',
  loadingKey: 'roles',
  addDangerToast: jest.fn(),
};

beforeEach(() => {
  jest.clearAllMocks();
});

test('returns all results in a single page when they fit in one request', async () => {
  const result = [
    { id: 1, name: 'Admin' },
    { id: 2, name: 'Gamma' },
  ];
  mockGet.mockResolvedValue({ json: { count: result.length, result } });
  const setData = jest.fn();

  await fetchPaginatedData({
    ...baseArgs,
    setData,
    setLoadingState: jest.fn(),
  });

  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(setData).toHaveBeenCalledWith(result);
});

test('applies mapResult to transform items into the mapped type', async () => {
  const result = [
    { id: 1, name: 'Admin' },
    { id: 2, name: 'Gamma' },
  ];
  mockGet.mockResolvedValue({ json: { count: result.length, result } });
  const setData = jest.fn();

  await fetchPaginatedData<
    { id: number; name: string },
    { value: number; label: string }
  >({
    ...baseArgs,
    setData,
    setLoadingState: jest.fn(),
    mapResult: item => ({ value: item.id, label: item.name }),
  });

  expect(setData).toHaveBeenCalledWith([
    { value: 1, label: 'Admin' },
    { value: 2, label: 'Gamma' },
  ]);
});

test('fetches and concatenates results across multiple pages', async () => {
  mockGet.mockImplementation(({ endpoint }: { endpoint: string }) => {
    const page = Number(
      decodeURIComponent(endpoint).match(/page:(\d+)/)?.[1] ?? 0,
    );
    return Promise.resolve({
      json: { count: 3, result: [{ id: page }] },
    });
  });
  const setData = jest.fn();

  await fetchPaginatedData({
    ...baseArgs,
    pageSize: 1,
    setData,
    setLoadingState: jest.fn(),
  });

  expect(mockGet).toHaveBeenCalledTimes(3);
  expect(setData).toHaveBeenCalledWith([{ id: 0 }, { id: 1 }, { id: 2 }]);
});

test('shows a danger toast and does not set data on error', async () => {
  mockGet.mockRejectedValue(new Error('boom'));
  const setData = jest.fn();
  const addDangerToast = jest.fn();

  await fetchPaginatedData({
    ...baseArgs,
    setData,
    setLoadingState: jest.fn(),
    addDangerToast,
    errorMessage: 'Error while fetching roles',
  });

  expect(addDangerToast).toHaveBeenCalledWith('Error while fetching roles');
  expect(setData).not.toHaveBeenCalled();
});

test('clears a boolean loading state in the finally block', async () => {
  mockGet.mockResolvedValue({ json: { count: 0, result: [] } });
  const setLoadingState = jest.fn();

  await fetchPaginatedData({
    ...baseArgs,
    setData: jest.fn(),
    setLoadingState,
  });

  const updater = setLoadingState.mock.calls[0][0];
  expect(updater(true)).toBe(false);
});

test('clears the keyed entry of a record loading state in the finally block', async () => {
  mockGet.mockResolvedValue({ json: { count: 0, result: [] } });
  const setLoadingState = jest.fn();

  await fetchPaginatedData({
    ...baseArgs,
    loadingKey: 'roles',
    setData: jest.fn(),
    setLoadingState,
  });

  const updater = setLoadingState.mock.calls[0][0];
  expect(updater({ roles: true, groups: true })).toEqual({
    roles: false,
    groups: true,
  });
});
