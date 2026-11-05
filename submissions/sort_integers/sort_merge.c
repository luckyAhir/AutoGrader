/*
 * sort_merge.c - sorts n integers in non-decreasing order with merge sort.
 * Input:  n, then n integers.   Output: the sorted values on one line.
 * Time O(n log n), extra memory O(n).
 */
#include <stdio.h>
#include <stdlib.h>

/* Merges the sorted runs values[left..mid) and values[mid..right) via scratch. */
static void merge(int *values, int *scratch, size_t left, size_t mid, size_t right)
{
    size_t i = left, j = mid, k = left;

    while (i < mid && j < right) {
        if (values[i] <= values[j]) {
            scratch[k++] = values[i++];
        } else {
            scratch[k++] = values[j++];
        }
    }
    while (i < mid) {
        scratch[k++] = values[i++];
    }
    while (j < right) {
        scratch[k++] = values[j++];
    }
    for (k = left; k < right; k++) {
        values[k] = scratch[k];
    }
}

/* Sorts values[left..right) recursively. */
static void merge_sort(int *values, int *scratch, size_t left, size_t right)
{
    if (right - left < 2) {
        return;
    }
    size_t mid = left + (right - left) / 2;
    merge_sort(values, scratch, left, mid);
    merge_sort(values, scratch, mid, right);
    merge(values, scratch, left, mid, right);
}

/* Reads count integers into a new array; returns NULL on bad input or no memory. */
static int *read_values(size_t count)
{
    int *values = malloc((count > 0 ? count : 1) * sizeof *values);

    if (values == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < count; i++) {
        if (scanf("%d", &values[i]) != 1) {
            free(values);
            return NULL;
        }
    }
    return values;
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
        fprintf(stderr, "error: expected a non-negative count\n");
        return EXIT_FAILURE;
    }
    size_t length = (size_t)count;
    int *values = read_values(length);
    int *scratch = malloc((length > 0 ? length : 1) * sizeof *scratch);
    if (values == NULL || scratch == NULL) {
        free(values);
        free(scratch);
        fprintf(stderr, "error: could not read the input\n");
        return EXIT_FAILURE;
    }
    merge_sort(values, scratch, 0, length);
    print_values(values, length);
    free(scratch);
    free(values);
    return EXIT_SUCCESS;
}
