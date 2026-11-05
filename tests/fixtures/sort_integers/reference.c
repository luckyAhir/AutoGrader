/* Reference solution for sort_integers (instructor's oracle). */
#include <stdio.h>
#include <stdlib.h>

/* Three-way comparison without subtraction, so INT_MIN/INT_MAX cannot overflow. */
static int compare_ints(const void *lhs, const void *rhs)
{
    int x = *(const int *)lhs;
    int y = *(const int *)rhs;
    return (x > y) - (x < y);
}

int main(void)
{
    int count;
    if (scanf("%d", &count) != 1 || count < 0) {
        return 1;
    }
    int *values = malloc((size_t)(count > 0 ? count : 1) * sizeof *values);
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; i++) {
        if (scanf("%d", &values[i]) != 1) {
            free(values);
            return 1;
        }
    }
    qsort(values, (size_t)count, sizeof *values, compare_ints);
    for (int i = 0; i < count; i++) {
        printf(i == 0 ? "%d" : " %d", values[i]);
    }
    printf("\n");
    free(values);
    return 0;
}
