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
import { isIterable } from 'src/utils/types';

test('isIterable returns true for iterable values', () => {
  expect(isIterable([])).toBe(true);
  expect(isIterable([1, 2, 3])).toBe(true);
  expect(isIterable('string')).toBe(true);
  expect(isIterable(new Set([1, 2]))).toBe(true);
  expect(isIterable(new Map())).toBe(true);
});

test('isIterable returns false for non-iterable values', () => {
  expect(isIterable(null)).toBe(false);
  expect(isIterable(undefined)).toBe(false);
  expect(isIterable(42)).toBe(false);
  expect(isIterable({})).toBe(false);
  expect(isIterable({ length: 3 })).toBe(false);
  expect(isIterable(true)).toBe(false);
});
