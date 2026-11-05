/*
 * sort_memmove_insertion.c - limitation demo: O(n^2) work hidden inside libc.
 * Binary search finds the insertion point (O(log n) instrumented lines per element),
 * memmove shifts the tail (O(n) per element, but inside libc, invisible to gcov).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Returns the first index in sorted[0..count) whose value is greater than key. */
static size_t upper_bound(const int *sorted, size_t count, int key)
{
    size_t low = 0, high = count;
    while (low < high) {
        size_t mid = low + (high - low) / 2;
        if (sorted[mid] <= key) {
            low = mid + 1;
        } else {
            high = mid;
        }
    }
    return low;
}

/* Inserts every value into the sorted prefix, shifting with memmove. */
static void insertion_sort(int *values, size_t count)
{
    for (size_t i = 1; i < count; i++) {
        int key = values[i];
        size_t pos = upper_bound(values, i, key);
        memmove(&values[pos + 1], &values[pos], (i - pos) * sizeof *values);
        values[pos] = key;
    }
}

/* Prints the values separated by single spaces, then a newline. */
static void print_values(const int *values, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        printf(i == 0 ? "%d" : " %d", values[i]);
    }
    printf("\n");
}

int main(void)
{
    int count;
    if (scanf("%d", &count) != 1 || count < 0) {
        return 1;
    }
    int *values = malloc(((size_t)count > 0 ? (size_t)count : 1) * sizeof *values);
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; i++) {
        if (scanf("%d", &values[i]) != 1) {
            free(values);
            return 1;
        }
    }
    insertion_sort(values, (size_t)count);
    print_values(values, (size_t)count);
    free(values);
    return 0;
}
